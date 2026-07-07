"""Tests for series-pass cancellation.

Two behaviors under test:
1. A ticket materialized from a series pass can never be self-cancelled by its
   holder — ``_block_reason`` short-circuits with ``PART_OF_SERIES_PASS`` even
   when the tier itself allows user cancellation.
2. ``series_pass_service.cancel_held_pass`` lets an organizer cancel the whole
   pass at once: future, non-checked-in tickets are cancelled and refunded;
   checked-in and past-event tickets are left untouched.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    Payment,
    SeriesPass,
    Ticket,
    TicketTier,
)
from events.models.ticket import CancellationBlockReason, CancellationSource
from events.service import cancellation_service
from events.service.series_pass_service import cancel_held_pass

pytestmark = pytest.mark.django_db


def _make_event(organization: Organization, event_series: EventSeries, name: str, slug: str, start: t.Any) -> Event:
    return Event.objects.create(
        organization=organization,
        name=name,
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=start,
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


def _make_tier(
    event: Event,
    name: str,
    payment_method: str = TicketTier.PaymentMethod.ONLINE,
    price: Decimal = Decimal("10.00"),
) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=price,
        currency="EUR",
        payment_method=payment_method,
    )


def _make_payment(ticket: Ticket, amount: Decimal, status: str = Payment.PaymentStatus.SUCCEEDED) -> Payment:
    return Payment.objects.create(
        ticket=ticket,
        user=ticket.user,
        stripe_session_id=f"cs_{ticket.id}",
        stripe_payment_intent_id=f"pi_{ticket.id}",
        amount=amount,
        platform_fee=Decimal("0.50"),
        currency="EUR",
        status=status,
    )


@pytest.fixture
def online_series_pass(event_series: EventSeries) -> SeriesPass:
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Online Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


@pytest.fixture
def held_pass(online_series_pass: SeriesPass, revel_user: RevelUser) -> HeldSeriesPass:
    return HeldSeriesPass.objects.create(
        series_pass=online_series_pass,
        user=revel_user,
        price_paid=Decimal("20.00"),
        status=HeldSeriesPass.Status.ACTIVE,
    )


class TestSelfCancelGuard:
    """A pass ticket can never be self-cancelled, regardless of tier settings."""

    def test_quote_cancellation_blocks_pass_ticket_even_when_tier_allows_cancellation(
        self,
        organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        held_pass: HeldSeriesPass,
    ) -> None:
        event = _make_event(organization, event_series, "Pass Event", "pass-event", timezone.now() + timedelta(days=5))
        tier = _make_tier(event, "Pass Tier")
        tier.allow_user_cancellation = True
        tier.save(update_fields=["allow_user_cancellation"])
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user,
            held_pass=held_pass,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=revel_user.get_display_name(),
        )

        quote = cancellation_service.quote_cancellation(ticket, timezone.now())

        assert quote.can_cancel is False
        assert quote.reason == CancellationBlockReason.PART_OF_SERIES_PASS

    def test_cancel_ticket_by_user_raises_with_pass_reason(
        self,
        organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        held_pass: HeldSeriesPass,
    ) -> None:
        event = _make_event(
            organization, event_series, "Pass Event 2", "pass-event-2", timezone.now() + timedelta(days=5)
        )
        tier = _make_tier(event, "Pass Tier 2")
        tier.allow_user_cancellation = True
        tier.save(update_fields=["allow_user_cancellation"])
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user,
            held_pass=held_pass,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=revel_user.get_display_name(),
        )

        with pytest.raises(cancellation_service.CancellationBlocked) as info:
            cancellation_service.cancel_ticket_by_user(ticket, revel_user, reason="", now=timezone.now())

        assert info.value.reason == CancellationBlockReason.PART_OF_SERIES_PASS


class _PassSetup(t.NamedTuple):
    held_pass: HeldSeriesPass
    future_tickets: list[Ticket]
    future_tiers: list[TicketTier]
    future_amounts: list[Decimal]
    checked_in_ticket: Ticket
    checked_in_tier: TicketTier
    past_ticket: Ticket
    past_tier: TicketTier


@pytest.fixture
def online_pass_setup(
    organization: Organization,
    event_series: EventSeries,
    revel_user: RevelUser,
    held_pass: HeldSeriesPass,
) -> _PassSetup:
    """An ACTIVE online-paid pass with: 2 future active tickets (refundable),
    1 checked-in future ticket, and 1 past-event ticket — all online-paid.
    """
    now = timezone.now()
    future_events = [
        _make_event(organization, event_series, f"Future {i}", f"future-{i}", now + timedelta(days=i + 1))
        for i in range(2)
    ]
    future_amounts = [Decimal("8.00"), Decimal("12.00")]
    future_tiers = [_make_tier(event, f"Future Tier {i}") for i, event in enumerate(future_events)]
    future_tickets = []
    for event, tier, amount in zip(future_events, future_tiers, future_amounts):
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user,
            held_pass=held_pass,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=revel_user.get_display_name(),
        )
        _make_payment(ticket, amount)
        tier.quantity_sold = 1
        tier.save(update_fields=["quantity_sold"])
        future_tickets.append(ticket)

    checked_in_event = _make_event(organization, event_series, "Checked In", "checked-in", now + timedelta(days=3))
    checked_in_tier = _make_tier(checked_in_event, "Checked In Tier")
    checked_in_ticket = Ticket.objects.create(
        event=checked_in_event,
        tier=checked_in_tier,
        user=revel_user,
        held_pass=held_pass,
        status=Ticket.TicketStatus.CHECKED_IN,
        checked_in_at=now,
        guest_name=revel_user.get_display_name(),
    )
    _make_payment(checked_in_ticket, Decimal("10.00"))
    checked_in_tier.quantity_sold = 1
    checked_in_tier.save(update_fields=["quantity_sold"])

    past_event = _make_event(organization, event_series, "Past", "past", now - timedelta(days=1))
    past_tier = _make_tier(past_event, "Past Tier")
    past_ticket = Ticket.objects.create(
        event=past_event,
        tier=past_tier,
        user=revel_user,
        held_pass=held_pass,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=revel_user.get_display_name(),
    )
    _make_payment(past_ticket, Decimal("10.00"))
    past_tier.quantity_sold = 1
    past_tier.save(update_fields=["quantity_sold"])

    return _PassSetup(
        held_pass=held_pass,
        future_tickets=future_tickets,
        future_tiers=future_tiers,
        future_amounts=future_amounts,
        checked_in_ticket=checked_in_ticket,
        checked_in_tier=checked_in_tier,
        past_ticket=past_ticket,
        past_tier=past_tier,
    )


class TestCancelHeldPassOnlinePaid:
    def test_future_tickets_cancelled_with_organizer_source_and_reason(
        self, online_pass_setup: _PassSetup, organization_owner_user: RevelUser
    ) -> None:
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_x"
            result = cancel_held_pass(
                online_pass_setup.held_pass, cancelled_by=organization_owner_user, reason="event dropped"
            )

        assert result.status == HeldSeriesPass.Status.CANCELLED
        for ticket in online_pass_setup.future_tickets:
            ticket.refresh_from_db()
            assert ticket.status == Ticket.TicketStatus.CANCELLED
            assert ticket.cancellation_source == CancellationSource.ORGANIZER
            assert ticket.cancelled_by_id == organization_owner_user.id
            assert ticket.cancellation_reason == "event dropped"
            assert ticket.cancelled_at is not None

    def test_one_refund_per_future_payment_at_payment_amount(
        self, online_pass_setup: _PassSetup, organization_owner_user: RevelUser
    ) -> None:
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_x"
            cancel_held_pass(online_pass_setup.held_pass, cancelled_by=organization_owner_user)

        # Only the two future, non-checked-in tickets are refunded.
        assert mock_create.call_count == 2
        refunded_amounts = sorted(call.kwargs["amount"] for call in mock_create.call_args_list)
        expected_amounts = sorted(int(amount * 100) for amount in online_pass_setup.future_amounts)
        assert refunded_amounts == expected_amounts

    def test_future_tiers_quantity_sold_decremented(
        self, online_pass_setup: _PassSetup, organization_owner_user: RevelUser
    ) -> None:
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_x"
            cancel_held_pass(online_pass_setup.held_pass, cancelled_by=organization_owner_user)

        for tier in online_pass_setup.future_tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0

    def test_checked_in_and_past_tickets_untouched(
        self, online_pass_setup: _PassSetup, organization_owner_user: RevelUser
    ) -> None:
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_x"
            cancel_held_pass(online_pass_setup.held_pass, cancelled_by=organization_owner_user)

        online_pass_setup.checked_in_ticket.refresh_from_db()
        assert online_pass_setup.checked_in_ticket.status == Ticket.TicketStatus.CHECKED_IN
        online_pass_setup.checked_in_tier.refresh_from_db()
        assert online_pass_setup.checked_in_tier.quantity_sold == 1

        online_pass_setup.past_ticket.refresh_from_db()
        assert online_pass_setup.past_ticket.status == Ticket.TicketStatus.ACTIVE
        online_pass_setup.past_tier.refresh_from_db()
        assert online_pass_setup.past_tier.quantity_sold == 1

        # No refund attempted for the checked-in or past-event ticket's payments.
        assert mock_create.call_count == 2


class TestCancelHeldPassCounters:
    def test_cancel_decrements_pass_quantity_sold_and_frees_repurchase(
        self, online_pass_setup: _PassSetup, online_series_pass: SeriesPass, organization_owner_user: RevelUser
    ) -> None:
        SeriesPass.objects.filter(pk=online_series_pass.pk).update(quantity_sold=1)

        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_x"
            cancel_held_pass(online_pass_setup.held_pass, cancelled_by=organization_owner_user)

        online_series_pass.refresh_from_db()
        assert online_series_pass.quantity_sold == 0
        # The conditional unique constraint no longer blocks a re-purchase.
        new_held = HeldSeriesPass.objects.create(
            series_pass=online_series_pass,
            user=online_pass_setup.held_pass.user,
            price_paid=Decimal("20.00"),
            status=HeldSeriesPass.Status.PENDING,
        )
        assert new_held.pk != online_pass_setup.held_pass.pk

    def test_double_cancel_is_idempotent(
        self, online_pass_setup: _PassSetup, online_series_pass: SeriesPass, organization_owner_user: RevelUser
    ) -> None:
        # quantity_sold=2 (as if another holder exists) pins the no-double-decrement
        # assertion: with 1 the floor guard alone would mask a second decrement.
        SeriesPass.objects.filter(pk=online_series_pass.pk).update(quantity_sold=2)

        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_x"
            cancel_held_pass(online_pass_setup.held_pass, cancelled_by=organization_owner_user)
            result = cancel_held_pass(online_pass_setup.held_pass, cancelled_by=organization_owner_user)

        assert result.status == HeldSeriesPass.Status.CANCELLED
        # Second call is a no-op: no extra refunds, no double counter decrement.
        assert mock_create.call_count == 2
        online_series_pass.refresh_from_db()
        assert online_series_pass.quantity_sold == 1
        for tier in online_pass_setup.future_tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0

    def test_cancel_with_stale_instance_rechecks_status_under_lock(
        self, online_pass_setup: _PassSetup, online_series_pass: SeriesPass, organization_owner_user: RevelUser
    ) -> None:
        """Concurrent-cancel shape: the second caller holds a stale instance that still
        says ACTIVE, so it passes the unlocked fast-path check — the locked re-read
        must catch the committed CANCELLED and no-op (no second decrement/refunds)."""
        SeriesPass.objects.filter(pk=online_series_pass.pk).update(quantity_sold=2)
        stale = HeldSeriesPass.objects.select_related("series_pass", "user").get(pk=online_pass_setup.held_pass.pk)

        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_x"
            cancel_held_pass(online_pass_setup.held_pass, cancelled_by=organization_owner_user)
            assert stale.status == HeldSeriesPass.Status.ACTIVE  # stale in memory
            result = cancel_held_pass(stale, cancelled_by=organization_owner_user)

        assert result.status == HeldSeriesPass.Status.CANCELLED
        assert mock_create.call_count == 2  # only the first call refunded
        online_series_pass.refresh_from_db()
        assert online_series_pass.quantity_sold == 1
        for tier in online_pass_setup.future_tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0


class TestCancelHeldPassOfflineTierRefund:
    def test_online_paid_pass_on_offline_tier_still_refunded(
        self,
        organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        held_pass: HeldSeriesPass,
        organization_owner_user: RevelUser,
    ) -> None:
        """The Payment row, not the mapped tier's payment method, gates the refund."""
        event = _make_event(
            organization, event_series, "Offline Tier Event", "offline-tier-event", timezone.now() + timedelta(days=2)
        )
        tier = _make_tier(event, "Offline Tier", payment_method=TicketTier.PaymentMethod.OFFLINE)
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user,
            held_pass=held_pass,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=revel_user.get_display_name(),
        )
        _make_payment(ticket, Decimal("9.00"))

        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_offline_tier"
            cancel_held_pass(held_pass, cancelled_by=organization_owner_user)

        assert mock_create.call_count == 1
        assert mock_create.call_args.kwargs["amount"] == 900
        payment = Payment.objects.get(ticket=ticket)
        assert payment.stripe_refund_id == "re_offline_tier"


class TestCancelPendingOnlinePass:
    @pytest.fixture
    def pending_online_setup(
        self,
        organization: Organization,
        event_series: EventSeries,
        online_series_pass: SeriesPass,
        revel_user: RevelUser,
    ) -> tuple[HeldSeriesPass, Ticket, Payment]:
        organization.stripe_account_id = "acct_org_cancel"
        organization.save(update_fields=["stripe_account_id"])
        held_pass = HeldSeriesPass.objects.create(
            series_pass=online_series_pass,
            user=revel_user,
            price_paid=Decimal("20.00"),
            status=HeldSeriesPass.Status.PENDING,
            stripe_session_id="cs_pending_cancel",
        )
        event = _make_event(
            organization, event_series, "Pending Event", "pending-event", timezone.now() + timedelta(days=2)
        )
        tier = _make_tier(event, "Pending Tier")
        tier.quantity_sold = 1
        tier.save(update_fields=["quantity_sold"])
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=revel_user,
            held_pass=held_pass,
            status=Ticket.TicketStatus.PENDING,
            guest_name=revel_user.get_display_name(),
        )
        payment = Payment.objects.create(
            ticket=ticket,
            user=revel_user,
            stripe_session_id="cs_pending_cancel",
            amount=Decimal("20.00"),
            platform_fee=Decimal("0.50"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
        )
        return held_pass, ticket, payment

    def test_cancel_pending_pass_expires_stripe_session_with_connected_account(
        self,
        pending_online_setup: tuple[HeldSeriesPass, Ticket, Payment],
        organization_owner_user: RevelUser,
    ) -> None:
        held_pass, ticket, payment = pending_online_setup

        with (
            patch("stripe.checkout.Session.expire") as mock_expire,
            patch("stripe.Refund.create") as mock_refund,
        ):
            cancel_held_pass(held_pass, cancelled_by=organization_owner_user)

        mock_expire.assert_called_once_with("cs_pending_cancel", stripe_account="acct_org_cancel")
        mock_refund.assert_not_called()

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.CANCELLED
        ticket.refresh_from_db()
        assert ticket.status == Ticket.TicketStatus.CANCELLED
        # Pending payment is failed so the expiry sweep can't double-release the tier.
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.FAILED
        ticket.tier.refresh_from_db()
        assert ticket.tier.quantity_sold == 0

    def test_cancel_pending_pass_tolerates_already_expired_session(
        self,
        pending_online_setup: tuple[HeldSeriesPass, Ticket, Payment],
        organization_owner_user: RevelUser,
    ) -> None:
        held_pass, _, _ = pending_online_setup

        import stripe as stripe_module

        with patch(
            "stripe.checkout.Session.expire",
            side_effect=stripe_module.error.InvalidRequestError("already expired", param=None),
        ):
            result = cancel_held_pass(held_pass, cancelled_by=organization_owner_user)

        assert result.status == HeldSeriesPass.Status.CANCELLED


class TestCancelHeldPassFree:
    def test_free_pass_cancel_issues_no_refunds(
        self,
        organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        now = timezone.now()
        free_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="Free Pass",
            price=Decimal("0.00"),
            pro_rata_discount=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        held_pass = HeldSeriesPass.objects.create(
            series_pass=free_pass,
            user=revel_user,
            price_paid=Decimal("0.00"),
            status=HeldSeriesPass.Status.ACTIVE,
        )
        events = [
            _make_event(organization, event_series, f"Free {i}", f"free-{i}", now + timedelta(days=i + 1))
            for i in range(2)
        ]
        tiers = [
            _make_tier(event, f"Free Tier {i}", payment_method=TicketTier.PaymentMethod.FREE, price=Decimal("0.00"))
            for i, event in enumerate(events)
        ]
        tickets = []
        for event, tier in zip(events, tiers):
            ticket = Ticket.objects.create(
                event=event,
                tier=tier,
                user=revel_user,
                held_pass=held_pass,
                status=Ticket.TicketStatus.ACTIVE,
                guest_name=revel_user.get_display_name(),
            )
            tier.quantity_sold = 1
            tier.save(update_fields=["quantity_sold"])
            tickets.append(ticket)

        with patch("stripe.Refund.create") as mock_create:
            result = cancel_held_pass(held_pass, cancelled_by=organization_owner_user)

        assert mock_create.call_count == 0
        assert result.status == HeldSeriesPass.Status.CANCELLED
        for ticket in tickets:
            ticket.refresh_from_db()
            assert ticket.status == Ticket.TicketStatus.CANCELLED
            assert ticket.cancellation_source == CancellationSource.ORGANIZER
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
