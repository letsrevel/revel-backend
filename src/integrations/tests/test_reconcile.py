"""Reconcile is ordered by staleness, respects the shared budget, and never crashes the sweep on one link."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.utils import timezone

from events.models import Event, TicketTier
from integrations.enums import IntegrationErrorCode
from integrations.exceptions import ProviderError
from integrations.models import EventLink, PlatformConnection, TierLink, WebhookDelivery
from integrations.service import connection_service, reconcile_service, sync_service
from integrations.tests.fake_provider import FakeProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def connected(organization, organization_owner_user, fake_provider: FakeProvider) -> PlatformConnection:  # type: ignore[no-untyped-def]
    start = connection_service.begin_connect(organization, organization_owner_user, "fake")
    return connection_service.complete_connect(start.state, "c")


def _live_link(organization, connected: PlatformConnection, name: str, *, days: int = 30) -> EventLink:  # type: ignore[no-untyped-def]
    start = timezone.now() + timedelta(days=days)
    event = Event.objects.create(
        organization=organization,
        name=name,
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=start,
        end=start + timedelta(hours=2),
        requires_ticket=True,
    )
    event.ticket_tiers.all().delete()
    TicketTier.objects.create(
        event=event, name="GA", price=Decimal("10"), total_quantity=50, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    link = sync_service.push_link(sync_service.ensure_link(event, connected))
    link.remote_status = EventLink.RemoteStatus.LIVE
    link.save(update_fields=["remote_status"])
    return link


def test_due_links_exclude_past_draft_broken_and_inactive(  # type: ignore[no-untyped-def]
    organization, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    live = _live_link(organization, connected, "Live")
    _live_link(organization, connected, "Past", days=-3)
    draft = _live_link(organization, connected, "Draft")
    draft.remote_status = EventLink.RemoteStatus.DRAFT
    draft.save(update_fields=["remote_status"])
    broken = _live_link(organization, connected, "Broken")
    broken.sync_state = EventLink.SyncState.BROKEN
    broken.save(update_fields=["sync_state"])
    assert list(reconcile_service.links_due_for_reconcile().values_list("id", flat=True)) == [live.id]
    connection_service.mark_revoked(connected)
    assert not reconcile_service.links_due_for_reconcile().exists()


def test_zero_tier_links_are_excluded_from_the_sweep(  # type: ignore[no-untyped-def]
    organization, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    start = timezone.now() + timedelta(days=30)
    event = Event.objects.create(
        organization=organization,
        name="PWYC-only",
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=start,
        end=start + timedelta(hours=2),
        requires_ticket=True,
    )
    event.ticket_tiers.all().delete()
    TicketTier.objects.create(
        event=event, name="PWYC", price_type=TicketTier.PriceType.PWYC, payment_method=TicketTier.PaymentMethod.ONLINE
    )
    pwyc_link = sync_service.push_link(sync_service.ensure_link(event, connected))
    pwyc_link.remote_status = EventLink.RemoteStatus.LIVE
    pwyc_link.save(update_fields=["remote_status"])
    assert not TierLink.objects.filter(event_link=pwyc_link).exists()  # unmappable tier, no TierLink
    has_tier = _live_link(organization, connected, "Has tier")
    assert list(reconcile_service.links_due_for_reconcile().values_list("id", flat=True)) == [has_tier.id]


def test_reconcile_orders_by_staleness_and_refreshes(  # type: ignore[no-untyped-def]
    organization, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    fresh = _live_link(organization, connected, "Fresh")
    stale = _live_link(organization, connected, "Stale")
    TierLink.objects.filter(event_link=fresh).update(counts_updated_at=timezone.now())
    for c in fake_provider.events[stale.remote_id].ticket_classes:
        c.quantity_sold = 9
    summary = reconcile_service.reconcile_counts()
    assert summary.refreshed == 2 and summary.skipped_for_budget == 0 and summary.failed == 0
    ids = [c[1] for c in fake_provider.calls if c[0] == "get_event"][-2:]
    assert ids == [stale.remote_id, fresh.remote_id]
    assert TierLink.objects.get(event_link=stale).remote_quantity_sold == 9


def test_reconcile_stops_when_budget_is_below_reserve(  # type: ignore[no-untyped-def]
    organization, connected: PlatformConnection, fake_provider: FakeProvider, settings
) -> None:
    _live_link(organization, connected, "A")
    _live_link(organization, connected, "B")
    settings.INTEGRATIONS_RATE_RESERVE = 200
    fake_provider.budget = 150
    before = [c for c in fake_provider.calls if c[0] == "get_event"]
    summary = reconcile_service.reconcile_counts()
    assert summary.refreshed == 0 and summary.skipped_for_budget == 2
    assert [c for c in fake_provider.calls if c[0] == "get_event"] == before


def test_reconcile_unknown_budget_proceeds(  # type: ignore[no-untyped-def]
    organization, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    _live_link(organization, connected, "A")
    fake_provider.budget = None
    assert reconcile_service.reconcile_counts().refreshed == 1


def test_reconcile_transient_error_stops_provider_other_errors_continue(  # type: ignore[no-untyped-def]
    organization, connected: PlatformConnection, fake_provider: FakeProvider
) -> None:
    _live_link(organization, connected, "A")
    _live_link(organization, connected, "B")
    fake_provider.fail_once = {"get_event": ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "bad")}
    summary = reconcile_service.reconcile_counts()
    assert (summary.refreshed, summary.failed) == (1, 1)
    fake_provider.fail["get_event"] = ProviderError(IntegrationErrorCode.PROVIDER_RATE_LIMITED, "429", retryable=True)
    summary = reconcile_service.reconcile_counts()
    assert summary.refreshed == 0 and summary.failed == 1  # stopped after the first transient failure


def test_prune_deletes_only_old_deliveries(connected: PlatformConnection) -> None:
    old = WebhookDelivery.objects.create(connection=connected, action="x")
    WebhookDelivery.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - timedelta(days=settings.INTEGRATIONS_WEBHOOK_DELIVERY_RETENTION_DAYS + 1)
    )
    WebhookDelivery.objects.create(connection=connected, action="y")
    assert reconcile_service.prune_webhook_deliveries() == 1
    assert WebhookDelivery.objects.count() == 1
