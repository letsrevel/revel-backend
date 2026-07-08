"""Tests for activation-time backfill of series-pass extensions.

The extension task (``materialize_series_pass_holders``) only processes ACTIVE
holders, so a buyer whose pass sat PENDING while the organizer extended the
series must be caught up when the pass activates — via the
``checkout.session.completed`` webhook (online) or ``confirm_held_pass_payment``
(offline). Also covers the locked PENDING re-check in the confirm path (no
double notification on a repeat confirm).
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    Payment,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.service import series_pass_service
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.service.stripe_webhooks import StripeEventHandler
from events.tasks.series_pass import materialize_series_pass_holders

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_connected_organization(organization: Organization) -> Organization:
    """Organization with Stripe account connected."""
    organization.stripe_account_id = "acct_test123"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("3.00")
    organization.platform_fee_fixed = Decimal("0.50")
    organization.save()
    return organization


def _make_covered_event(
    organization: Organization,
    event_series: EventSeries,
    series_pass: SeriesPass,
    slug: str,
    start_offset: timedelta = timedelta(days=7),
) -> tuple[Event, TicketTier, SeriesPassTierLink]:
    event = Event.objects.create(
        organization=organization,
        name=f"Event {slug}",
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=timezone.now() + start_offset,
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )
    tier = TicketTier.objects.create(
        event=event,
        name=f"Tier {slug}",
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    link = SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return event, tier, link


def _make_pass(
    organization: Organization,
    event_series: EventSeries,
    payment_method: str,
    slug_prefix: str,
) -> SeriesPass:
    """A pass covering two future events (the quote requires >= 2 remaining)."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name=f"Backfill Pass {slug_prefix}",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=payment_method,
    )
    for i in range(2):
        _make_covered_event(organization, event_series, series_pass, f"{slug_prefix}-{i}", timedelta(days=i + 1))
    return series_pass


def _purchase_online(series_pass: SeriesPass, user: RevelUser, session_id: str) -> HeldSeriesPass:
    """Drive the real ONLINE purchase flow (mocked Stripe) to a PENDING HeldSeriesPass."""
    mock_session = Mock()
    mock_session.id = session_id
    mock_session.url = f"https://checkout.stripe.com/pay/{session_id}"
    with patch("stripe.checkout.Session.create", return_value=mock_session):
        SeriesPassPurchaseService(series_pass, user).purchase()
    return HeldSeriesPass.objects.get(series_pass=series_pass, user=user, status=HeldSeriesPass.Status.PENDING)


def _completed_checkout_event(session_id: str) -> MagicMock:
    """Build a fake, iterable ``checkout.session.completed`` stripe.Event."""
    session_data = {"id": session_id, "payment_status": "paid", "payment_intent": f"pi_{session_id}"}
    event_data = {"type": "checkout.session.completed", "data": {"object": session_data}}
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_data.items())
    mock_event.type = event_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = session_data
    return mock_event


class TestWebhookActivationBackfill:
    def test_extension_missed_while_pending_is_granted_on_webhook_activation(
        self,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Buy online (PENDING) -> extend series -> complete webhook -> new ticket ACTIVE + tier counted."""
        series_pass = _make_pass(stripe_connected_organization, event_series, TicketTier.PaymentMethod.ONLINE, "wb")
        held_pass = _purchase_online(series_pass, revel_user, "cs_backfill_online")

        new_event, new_tier, _ = _make_covered_event(stripe_connected_organization, event_series, series_pass, "wb-new")
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            materialize_series_pass_holders(str(series_pass.id), [str(new_event.id)])
        # The extension task skips the PENDING holder — this is the gap under test.
        assert not Ticket.objects.filter(held_pass=held_pass, event=new_event).exists()

        event = _completed_checkout_event("cs_backfill_online")
        with django_capture_on_commit_callbacks(execute=True):
            StripeEventHandler(event).handle_checkout_session_completed(event)

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.ACTIVE
        backfilled = Ticket.objects.get(held_pass=held_pass, event=new_event)
        assert backfilled.status == Ticket.TicketStatus.ACTIVE
        assert backfilled.tier_id == new_tier.id
        # Free of charge: no Payment row for the backfilled ticket.
        assert not Payment.objects.filter(ticket=backfilled).exists()
        new_tier.refresh_from_db()
        assert new_tier.quantity_sold == 1

    def test_webhook_without_missed_extension_backfills_nothing(
        self,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        series_pass = _make_pass(stripe_connected_organization, event_series, TicketTier.PaymentMethod.ONLINE, "wnb")
        held_pass = _purchase_online(series_pass, revel_user, "cs_backfill_noop")
        ticket_count = Ticket.objects.filter(held_pass=held_pass).count()

        event = _completed_checkout_event("cs_backfill_noop")
        with django_capture_on_commit_callbacks(execute=True):
            StripeEventHandler(event).handle_checkout_session_completed(event)

        assert Ticket.objects.filter(held_pass=held_pass).count() == ticket_count


class TestOfflineConfirmBackfill:
    def test_extension_missed_while_pending_is_granted_on_offline_confirm(
        self,
        organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Buy offline (PENDING) -> extend series -> organizer confirms -> new ticket ACTIVE + tier counted."""
        series_pass = _make_pass(organization, event_series, TicketTier.PaymentMethod.OFFLINE, "ob")
        result = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(result, HeldSeriesPass)
        held_pass = result
        assert held_pass.status == HeldSeriesPass.Status.PENDING

        new_event, new_tier, _ = _make_covered_event(organization, event_series, series_pass, "ob-new")
        with patch("notifications.signals.series_pass.send_series_pass_extended"):
            materialize_series_pass_holders(str(series_pass.id), [str(new_event.id)])
        assert not Ticket.objects.filter(held_pass=held_pass, event=new_event).exists()

        with patch("events.service.series_pass_service.send_series_pass_purchased"):
            with django_capture_on_commit_callbacks(execute=True):
                series_pass_service.confirm_held_pass_payment(held_pass)

        tickets = Ticket.objects.filter(held_pass=held_pass)
        assert tickets.count() == 3
        assert all(ticket.status == Ticket.TicketStatus.ACTIVE for ticket in tickets)
        assert tickets.filter(event=new_event, tier=new_tier).exists()
        new_tier.refresh_from_db()
        assert new_tier.quantity_sold == 1


class TestBackfillMissingTickets:
    """Unit tests for the service function itself (runs inside the test's transaction)."""

    def test_returns_empty_when_pass_fully_covered(
        self, organization: Organization, event_series: EventSeries, revel_user: RevelUser
    ) -> None:
        series_pass = _make_pass(organization, event_series, TicketTier.PaymentMethod.OFFLINE, "unit-full")
        result = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(result, HeldSeriesPass)

        assert series_pass_service.backfill_missing_tickets(result) == []

    def test_skips_full_tier_and_grants_the_rest(
        self, organization: Organization, event_series: EventSeries, revel_user: RevelUser
    ) -> None:
        series_pass = _make_pass(organization, event_series, TicketTier.PaymentMethod.OFFLINE, "unit-cap")
        result = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(result, HeldSeriesPass)

        _, full_tier, _ = _make_covered_event(organization, event_series, series_pass, "unit-cap-full")
        open_event, open_tier, _ = _make_covered_event(organization, event_series, series_pass, "unit-cap-open")
        TicketTier.objects.filter(pk=full_tier.pk).update(total_quantity=1, quantity_sold=1)

        created = series_pass_service.backfill_missing_tickets(result)

        assert [ticket.event_id for ticket in created] == [open_event.id]
        # Free of charge — a backfilled ticket must never inherit the mapped tier's
        # price in revenue/VAT reports (#644).
        assert all(ticket.price_paid == Decimal("0.00") for ticket in created)
        full_tier.refresh_from_db()
        assert full_tier.quantity_sold == 1  # unchanged — skipped, not granted
        open_tier.refresh_from_db()
        assert open_tier.quantity_sold == 1

    def test_ignores_past_events(
        self, organization: Organization, event_series: EventSeries, revel_user: RevelUser
    ) -> None:
        series_pass = _make_pass(organization, event_series, TicketTier.PaymentMethod.OFFLINE, "unit-past")
        result = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(result, HeldSeriesPass)

        past_event, past_tier, _ = _make_covered_event(
            organization, event_series, series_pass, "unit-past-ev", start_offset=timedelta(days=-1)
        )

        assert series_pass_service.backfill_missing_tickets(result) == []
        assert not Ticket.objects.filter(held_pass=result, event=past_event).exists()
        past_tier.refresh_from_db()
        assert past_tier.quantity_sold == 0


class TestConfirmHeldPassPaymentLockedRecheck:
    def test_repeat_confirm_with_stale_instance_400s_and_notifies_once(
        self,
        organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """The PENDING re-check runs under a row lock: a second confirm on a stale
        instance (still PENDING in memory) must 400 and must not re-notify."""
        series_pass = _make_pass(organization, event_series, TicketTier.PaymentMethod.OFFLINE, "cas")
        result = SeriesPassPurchaseService(series_pass, revel_user).purchase()
        assert isinstance(result, HeldSeriesPass)

        stale = HeldSeriesPass.objects.select_related("series_pass", "user").get(pk=result.pk)

        with patch("events.service.series_pass_service.send_series_pass_purchased") as mock_notify:
            with django_capture_on_commit_callbacks(execute=True):
                series_pass_service.confirm_held_pass_payment(result)

            assert stale.status == HeldSeriesPass.Status.PENDING  # stale in memory
            with pytest.raises(HttpError) as exc_info:
                series_pass_service.confirm_held_pass_payment(stale)

        assert exc_info.value.status_code == 400
        mock_notify.assert_called_once_with(result.pk)
