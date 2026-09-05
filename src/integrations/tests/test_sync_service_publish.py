"""Publish is explicit, synchronous, idempotent, and refuses broken or unpushed links."""

from decimal import Decimal

import pytest

from events.models import Event, TicketTier
from integrations import registry
from integrations.exceptions import IntegrationError, ProviderError
from integrations.models import EventLink, PlatformConnection, TierLink
from integrations.schema import IntegrationErrorCode
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
    return sync_service.push_link(sync_service.ensure_link(event, connected))


def test_publish_makes_live(pushed: EventLink, fake_provider: FakeProvider, connected: PlatformConnection) -> None:
    link = sync_service.publish_link(pushed.event, "fake")
    assert link.remote_status == EventLink.RemoteStatus.LIVE
    assert fake_provider.get_event(connected.token(), link.remote_id).status == "live"
    again = sync_service.publish_link(pushed.event, "fake")  # idempotent
    assert again.remote_status == EventLink.RemoteStatus.LIVE
    assert fake_provider.calls.count(("publish_event", link.remote_id)) == 1


def test_publish_without_push_404(event: Event, connected: PlatformConnection) -> None:
    with pytest.raises(IntegrationError) as exc:
        sync_service.publish_link(event, "fake")
    assert exc.value.status == 404 and exc.value.code == IntegrationErrorCode.PROVIDER_NOT_CONNECTED


def test_publish_broken_link_refused(pushed: EventLink) -> None:
    pushed.sync_state = EventLink.SyncState.BROKEN
    pushed.save(update_fields=["sync_state"])
    with pytest.raises(IntegrationError) as exc:
        sync_service.publish_link(pushed.event, "fake")
    assert exc.value.code == IntegrationErrorCode.REMOTE_EVENT_MISSING


def test_publish_provider_rejection_502(pushed: EventLink, fake_provider: FakeProvider) -> None:
    fake_provider.fail["publish_event"] = ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "Venue required")
    with pytest.raises(IntegrationError) as exc:
        sync_service.publish_link(pushed.event, "fake")
    assert exc.value.status == 502 and exc.value.provider_message == "Venue required"
    pushed.refresh_from_db()
    assert pushed.remote_status == EventLink.RemoteStatus.DRAFT


def test_publish_remote_missing_breaks_the_link(pushed: EventLink, fake_provider: FakeProvider) -> None:
    fake_provider.fail["publish_event"] = ProviderError(IntegrationErrorCode.REMOTE_EVENT_MISSING, "gone")
    with pytest.raises(IntegrationError) as exc:
        sync_service.publish_link(pushed.event, "fake")
    assert exc.value.status == 409 and exc.value.code == IntegrationErrorCode.REMOTE_EVENT_MISSING
    pushed.refresh_from_db()
    assert pushed.sync_state == EventLink.SyncState.BROKEN and pushed.remote_id == ""
    assert TierLink.objects.filter(event_link=pushed).count() == 0


def test_list_links_skips_a_disabled_provider(pushed: EventLink, monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(sync_service.list_links(pushed.event)) == 1
    monkeypatch.setattr(registry, "PROVIDERS", {})
    assert sync_service.list_links(pushed.event) == []


def test_set_link_auto_sync_and_schema(pushed: EventLink) -> None:
    link = sync_service.set_link_auto_sync(pushed.event, "fake", True)
    assert link.auto_sync is True and link.effective_auto_sync is True
    link = sync_service.set_link_auto_sync(pushed.event, "fake", None)
    assert link.auto_sync is None and link.effective_auto_sync is False
    rows = sync_service.list_links(pushed.event)
    assert len(rows) == 1
    row = rows[0]
    assert (row.provider, row.display_name, row.remote_status, row.sync_state, row.origin) == (
        "fake",
        "Fake",
        "draft",
        "in_sync",
        "pushed",
    )
    assert [tier.tier_name for tier in row.tiers] == ["GA"]
    assert row.sync_report[0].code == IntegrationErrorCode.IMAGE_MISSING
