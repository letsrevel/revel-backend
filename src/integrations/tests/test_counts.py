"""Count refresh reuses get_event; matches classes by remote id; handles revoked/missing/transient."""

from decimal import Decimal

import pytest

from events.models import Event, TicketTier
from integrations.enums import IntegrationErrorCode
from integrations.exceptions import ProviderError, RetryableProviderError
from integrations.models import EventLink, PlatformConnection, TierLink
from integrations.providers.base import RemoteTicketClass
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


def _sell(fake: FakeProvider, remote_event_id: str, name: str, n: int) -> None:
    for c in fake.events[remote_event_id].ticket_classes:
        if c.name == name:
            c.quantity_sold = n


def test_refresh_updates_matching_links_only(
    pushed: EventLink, fake_provider: FakeProvider, connected: PlatformConnection
) -> None:
    _sell(fake_provider, pushed.remote_id, "GA", 7)
    remote_only = RemoteTicketClass(
        name="Remote-only", price=Decimal("1"), currency="EUR", is_free=False, quantity_total=5, quantity_sold=3
    )
    fake_provider.upsert_ticket_class(connected.token(), pushed.remote_id, remote_only)
    assert sync_service.refresh_counts(pushed) is True
    by_name = {tl.tier.name: tl for tl in TierLink.objects.filter(event_link=pushed).select_related("tier") if tl.tier}
    assert by_name["GA"].remote_quantity_sold == 7 and by_name["VIP"].remote_quantity_sold == 0
    assert all(tl.counts_updated_at is not None for tl in by_name.values())
    assert TierLink.objects.filter(event_link=pushed).count() == 2  # remote-only class creates no link


def test_refresh_revoked_marks_connection(
    pushed: EventLink, fake_provider: FakeProvider, connected: PlatformConnection
) -> None:
    fake_provider.fail["get_event"] = ProviderError(IntegrationErrorCode.CONNECTION_REVOKED, "401")
    assert sync_service.refresh_counts(pushed) is False
    connected.refresh_from_db()
    assert connected.status == PlatformConnection.Status.ERROR


def test_refresh_missing_marks_broken(pushed: EventLink, fake_provider: FakeProvider) -> None:
    fake_provider.missing.add(pushed.remote_id)
    assert sync_service.refresh_counts(pushed) is False
    pushed.refresh_from_db()
    assert pushed.sync_state == EventLink.SyncState.BROKEN and pushed.remote_id == ""
    assert not TierLink.objects.filter(event_link=pushed).exists()


def test_refresh_retryable_raises(pushed: EventLink, fake_provider: FakeProvider) -> None:
    fake_provider.fail["get_event"] = ProviderError(IntegrationErrorCode.PROVIDER_RATE_LIMITED, "429", retryable=True)
    with pytest.raises(RetryableProviderError):
        sync_service.refresh_counts(pushed)
