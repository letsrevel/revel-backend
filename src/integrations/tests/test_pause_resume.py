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
from integrations.providers.base import TokenSet
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


@pytest.fixture
def pushed3(event: Event, connected: PlatformConnection) -> EventLink:
    """Three tiers so a mid-loop revocation leaves a genuinely untouched tier behind."""
    event.ticket_tiers.all().delete()
    for name, price, qty in (("GA", "10", 100), ("PREMIUM", "30", 50), ("VIP", "50", 10)):
        TicketTier.objects.create(
            event=event,
            name=name,
            price=Decimal(price),
            total_quantity=qty,
            payment_method=TicketTier.PaymentMethod.ONLINE,
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


def test_revoked_token_reports_the_untried_tiers_and_marks_the_connection(
    pushed3: EventLink,
    fake_provider: FakeProvider,
    connected: PlatformConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tier hit by the revocation and every tier after it in the loop must both show up as failed.

    ``fail_once`` fires on the *first* call to the method regardless of which tier it targets, so a
    plain call-counting stub is used here to fail specifically the second of three tiers (GA succeeds,
    PREMIUM is revoked, VIP is never attempted).
    """
    real = fake_provider.set_ticket_class_paused
    calls = {"n": 0}

    def flaky(token: TokenSet, remote_event_id: str, remote_id: str, paused: bool) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise ProviderError(IntegrationErrorCode.CONNECTION_REVOKED, "401")
        real(token, remote_event_id, remote_id, paused)

    monkeypatch.setattr(fake_provider, "set_ticket_class_paused", flaky)
    result = sync_service.set_remote_paused(pushed3.event, "fake", tier_id=None, paused=True)
    assert len(result.updated) == 1  # GA, alphabetically/display-order first
    assert len(result.failed) == 2  # PREMIUM (the failing call) + VIP (never attempted)
    assert {f.code for f in result.failed} == {IntegrationErrorCode.CONNECTION_REVOKED}
    assert {f.tier_name for f in result.failed} == {"PREMIUM", "VIP"}
    not_attempted = next(f for f in result.failed if f.tier_name == "VIP")
    assert not_attempted.provider_message is None
    assert calls["n"] == 2  # VIP's call was never made
    connected.refresh_from_db()
    assert connected.status == PlatformConnection.Status.ERROR


def test_unknown_tier_returns_tier_not_linked(pushed: EventLink) -> None:
    import uuid

    with pytest.raises(IntegrationError) as exc:
        sync_service.set_remote_paused(pushed.event, "fake", tier_id=uuid.uuid4(), paused=True)
    assert exc.value.status == 404
    assert exc.value.code == IntegrationErrorCode.TIER_NOT_LINKED


def test_other_event_tier_is_not_linked(pushed: EventLink, fake_provider: FakeProvider) -> None:
    """A tier belonging to a different event of the same organization must not be reachable here (IDOR)."""
    other_event = Event.objects.create(
        organization=pushed.event.organization,
        name="Other Event",
        slug="other-event",
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=pushed.event.start,
        end=pushed.event.end,
        requires_ticket=True,
    )
    other_tier = TicketTier.objects.create(
        event=other_event,
        name="GA",
        price=Decimal("10"),
        total_quantity=100,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    with pytest.raises(IntegrationError) as exc:
        sync_service.set_remote_paused(pushed.event, "fake", tier_id=other_tier.id, paused=True)
    assert exc.value.status == 404
    assert exc.value.code == IntegrationErrorCode.TIER_NOT_LINKED
    assert not any(c[0] == "set_ticket_class_paused" for c in fake_provider.calls)


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
