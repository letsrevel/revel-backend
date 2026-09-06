"""Remote pause/resume: synchronous, per tier or all, partial failures reported, remote_paused is the record."""

from decimal import Decimal

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, TicketTier
from integrations.enums import IntegrationErrorCode
from integrations.exceptions import IntegrationError, ProviderError
from integrations.models import EventLink, PlatformConnection, TierLink
from integrations.service import connection_service, sync_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def connected(organization, organization_owner_user, fake_provider: FakeProvider) -> PlatformConnection:  # type: ignore[no-untyped-def]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    return connection_service.complete_connect(start.state, "c")


@pytest.fixture
def pushed(event: Event, connected: PlatformConnection) -> EventLink:
    event.ticket_tiers.all().delete()
    TicketTier.objects.create(
        event=event, name="GA", price=Decimal("10"), total_quantity=100, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    TicketTier.objects.create(
        event=event, name="VIP", price=Decimal("50"), total_quantity=10, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    return sync_service.push_link(sync_service.ensure_link(event, connected))


def _hidden(fake: FakeProvider, remote_event_id: str) -> dict[str, bool]:
    return {c.name: c.hidden for c in fake.events[remote_event_id].ticket_classes}


def test_pause_all_then_resume_one(pushed: EventLink, fake_provider: FakeProvider) -> None:
    result = sync_service.set_remote_paused(pushed.event, "fake", tier_id=None, paused=True)
    assert result.paused is True and len(result.updated) == 2 and result.failed == []
    assert _hidden(fake_provider, pushed.remote_id) == {"GA": True, "VIP": True}
    assert set(TierLink.objects.filter(event_link=pushed).values_list("remote_paused", flat=True)) == {True}
    ga = pushed.event.ticket_tiers.get(name="GA")
    result = sync_service.set_remote_paused(pushed.event, "fake", tier_id=ga.id, paused=False)
    assert result.updated == [ga.id]
    assert _hidden(fake_provider, pushed.remote_id) == {"GA": False, "VIP": True}
    assert TierLink.objects.get(event_link=pushed, tier=ga).remote_paused is False


def test_pause_survives_the_next_push(pushed: EventLink, fake_provider: FakeProvider) -> None:
    sync_service.set_remote_paused(pushed.event, "fake", tier_id=None, paused=True)
    sync_service.push_link(pushed)
    assert _hidden(fake_provider, pushed.remote_id) == {"GA": True, "VIP": True}


def test_partial_failure_reported(pushed: EventLink, fake_provider: FakeProvider) -> None:
    fake_provider.fail_once = {"set_ticket_class_paused": ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "nope")}
    result = sync_service.set_remote_paused(pushed.event, "fake", tier_id=None, paused=True)
    assert len(result.updated) == 1 and len(result.failed) == 1
    assert result.failed[0].code == IntegrationErrorCode.PAUSE_FAILED and result.failed[0].provider_message == "nope"


def test_unknown_tier_404(pushed: EventLink) -> None:
    import uuid

    from django.http import Http404

    with pytest.raises(Http404):
        sync_service.set_remote_paused(pushed.event, "fake", tier_id=uuid.uuid4(), paused=True)


def test_requires_pushed_link(event: Event, connected: PlatformConnection) -> None:
    with pytest.raises(IntegrationError) as exc:
        sync_service.set_remote_paused(event, "fake", tier_id=None, paused=True)
    assert exc.value.status == 404


def test_endpoints(organization_owner_client: Client, pushed: EventLink, fake_provider: FakeProvider) -> None:
    url = reverse("api:event_integration_pause", kwargs={"event_id": pushed.event_id, "provider": "fake"})
    response = organization_owner_client.post(url, content_type="application/json")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["paused"] is True and len(body["updated"]) == 2 and body["link"]["tiers"][0]["remote_paused"] is True
    ga = pushed.event.ticket_tiers.get(name="GA")
    url = reverse("api:event_integration_resume", kwargs={"event_id": pushed.event_id, "provider": "fake"})
    response = organization_owner_client.post(
        url, data=orjson.dumps({"tier_id": str(ga.id)}), content_type="application/json"
    )
    assert response.status_code == 200 and response.json()["updated"] == [str(ga.id)]
