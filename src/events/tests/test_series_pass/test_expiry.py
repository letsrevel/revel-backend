"""Tests for pass-aware expiry of abandoned online series-pass checkouts.

Three expiry routes share ``expire_stranded_held_passes``: the
``cleanup_expired_payments`` beat task, the resume/cancel-checkout batch cleanup
(``_cleanup_expired_batch`` / ``cancel_pending_checkout``), and the
``payment_intent.canceled`` webhook. Each must flip the stranded PENDING pass to
CANCELLED, restore tier + pass counters, and let the buyer purchase again.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe
from django.db.models import F
from django.utils import timezone

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
from events.service import series_pass_service, stripe_service
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.service.stripe_webhooks import StripeEventHandler
from events.tasks.payments import cleanup_expired_payments

pytestmark = pytest.mark.django_db


@pytest.fixture
def online_pass_two_tiers(
    stripe_connected_organization: Organization, event_series: EventSeries
) -> tuple[SeriesPass, list[TicketTier]]:
    """An ONLINE pass covering two future events, one tier each."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Expiry Pass",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    now = timezone.now()
    tiers = []
    for i in range(2):
        event = Event.objects.create(
            organization=stripe_connected_organization,
            name=f"Expiry Event {i}",
            slug=f"expiry-event-{i}",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=100,
            start=now + timedelta(days=i + 1),
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name=f"Expiry Tier {i}",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
        tiers.append(tier)
    return series_pass, tiers


def _purchase(series_pass: SeriesPass, user: RevelUser, session_id: str) -> HeldSeriesPass:
    """Drive the real ONLINE purchase flow (reserve + session, mocked Stripe) to a PENDING HeldSeriesPass (#632)."""
    mock_session = Mock()
    mock_session.id = session_id
    mock_session.url = f"https://checkout.stripe.com/pay/{session_id}"
    with patch("stripe.checkout.Session.create", return_value=mock_session):
        _, reservation_id = SeriesPassPurchaseService(series_pass, user).purchase()  # type: ignore[misc]
        stripe_service.create_series_pass_session(reservation_id=reservation_id)
    return HeldSeriesPass.objects.get(
        series_pass=series_pass, user=user, status=HeldSeriesPass.HeldSeriesPassStatus.PENDING
    )


class TestBeatTaskExpiry:
    def test_expired_checkout_cancels_pass_restores_counters_and_allows_repurchase(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_beat")

        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 1
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 1

        Payment.objects.filter(stripe_session_id="cs_expiry_beat").update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        cleaned = cleanup_expired_payments()
        assert cleaned == 2

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        assert not Ticket.objects.filter(held_pass=held_pass).exists()

        # The buyer is no longer blocked by the conditional unique constraint.
        new_pass = _purchase(series_pass, revel_user, "cs_expiry_retry")
        assert new_pass.pk != held_pass.pk
        assert new_pass.status == HeldSeriesPass.HeldSeriesPassStatus.PENDING

    def test_beat_task_leaves_unexpired_pass_checkouts_alone(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, _ = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_fresh")

        cleaned = cleanup_expired_payments()
        assert cleaned == 0

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.PENDING


class TestCleanupExpiredBatch:
    def test_multi_tier_batch_decrements_each_tier_not_one(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        """A pass batch spans N tiers; the old code decremented ONE tier by N."""
        series_pass, tiers = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_batch")

        payment = Payment.objects.filter(stripe_session_id="cs_expiry_batch").select_related("ticket__tier").first()
        assert payment is not None

        stripe_service._cleanup_expired_batch(payment)

        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        assert not Payment.objects.filter(stripe_session_id="cs_expiry_batch").exists()

    def test_cancel_pending_checkout_releases_pass_and_all_tiers(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_user_cancel")
        payment = Payment.objects.filter(stripe_session_id="cs_expiry_user_cancel").first()
        assert payment is not None

        cancelled = stripe_service.cancel_pending_checkout(str(payment.id), revel_user)
        assert cancelled == 2

        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        assert not Ticket.objects.filter(held_pass=held_pass).exists()


def _canceled_intent_event(payment_intent_id: str) -> MagicMock:
    """Build a fake, iterable ``payment_intent.canceled`` stripe.Event."""
    intent_data = {"id": payment_intent_id}
    event_data = {"type": "payment_intent.canceled", "data": {"object": intent_data}}
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_data.items())
    mock_event.type = event_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = intent_data
    return mock_event


class TestExpireStrandedClaim:
    """Overlapping expiry routes may only release the pass counter once (atomic claim)."""

    def test_double_expiry_releases_pass_counter_exactly_once(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, _ = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_double_claim")

        first = series_pass_service.expire_stranded_held_passes(["cs_double_claim"])
        second = series_pass_service.expire_stranded_held_passes(["cs_double_claim"])

        assert (first, second) == (1, 0)
        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0

    def test_lost_claim_between_snapshot_and_update_does_not_double_decrement(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        """Race shape: this route snapshots the PENDING pass, then a concurrent route
        (webhook / user cancel) commits the cancel + counter release first. Losing the
        conditional-UPDATE claim must skip the decrement."""
        series_pass, _ = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_lost_claim")
        SeriesPass.objects.filter(pk=series_pass.pk).update(quantity_sold=2)  # as if another holder exists

        # The concurrent route wins: pass CANCELLED, counter released (2 -> 1).
        HeldSeriesPass.objects.filter(pk=held_pass.pk).update(status=HeldSeriesPass.HeldSeriesPassStatus.CANCELLED)
        SeriesPass.objects.filter(pk=series_pass.pk).update(quantity_sold=F("quantity_sold") - 1)

        manager = HeldSeriesPass.objects
        real_filter = manager.filter

        def stale_snapshot_filter(*args: t.Any, **kwargs: t.Any) -> t.Any:
            if "stripe_session_id__in" in kwargs:
                # Simulate the snapshot having been taken while the pass was still PENDING.
                return real_filter(pk=held_pass.pk)
            return real_filter(*args, **kwargs)

        with patch.object(manager, "filter", side_effect=stale_snapshot_filter):
            cancelled = series_pass_service.expire_stranded_held_passes(["cs_lost_claim"])

        assert cancelled == 0
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 1  # released exactly once, by the winner


class TestScopedSessionCleanup:
    """Session cleanup must only touch PENDING payments/tickets (review finding F11).

    Verified sequence: a pass covers a past and a future event; the organizer cancels
    the PENDING pass (future ticket -> CANCELLED + tier released + payment FAILED;
    past ticket untouched, its payment stays PENDING). The buyer then cancels the
    remaining pending payment — the old unscoped cleanup re-released the FAILED
    subset's tier (crossing zero -> IntegrityError 500) and hard-deleted the
    CANCELLED audit ticket.
    """

    def _organizer_then_buyer_setup(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
        organization_owner_user: RevelUser,
        session_id: str,
    ) -> tuple[SeriesPass, list[TicketTier], HeldSeriesPass, Ticket, Payment]:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, session_id)

        # One covered event slips into the past before the organizer cancels.
        Event.objects.filter(pk=tiers[0].event_id).update(start=timezone.now() - timedelta(days=1))

        with patch("stripe.checkout.Session.expire"):
            series_pass_service.cancel_held_pass(held_pass, cancelled_by=organization_owner_user)

        future_ticket = Ticket.objects.get(held_pass=held_pass, tier=tiers[1])
        assert future_ticket.status == Ticket.TicketStatus.CANCELLED
        past_payment = Payment.objects.get(stripe_session_id=session_id, status=Payment.PaymentStatus.PENDING)
        return series_pass, tiers, held_pass, future_ticket, past_payment

    def test_buyer_cancel_after_organizer_cancel_preserves_audit_and_counters(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        series_pass, tiers, held_pass, future_ticket, past_payment = self._organizer_then_buyer_setup(
            online_pass_two_tiers, revel_user, organization_owner_user, "cs_scoped_cancel"
        )

        # Must not raise (old code: tier double-release crossed zero -> IntegrityError).
        cancelled = stripe_service.cancel_pending_checkout(str(past_payment.id), revel_user)
        assert cancelled == 1

        # The organizer's CANCELLED audit ticket survives with its cancellation fields.
        future_ticket.refresh_from_db()
        assert future_ticket.status == Ticket.TicketStatus.CANCELLED
        assert future_ticket.cancelled_at is not None
        # The pending past-event ticket and its payment are gone; the FAILED payment survives.
        assert not Ticket.objects.filter(pk=past_payment.ticket_id).exists()
        assert not Payment.objects.filter(pk=past_payment.pk).exists()
        assert Payment.objects.filter(
            stripe_session_id="cs_scoped_cancel", status=Payment.PaymentStatus.FAILED
        ).exists()
        # Each tier released exactly once.
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0

    def test_cleanup_expired_batch_after_organizer_cancel_preserves_audit_and_counters(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        series_pass, tiers, held_pass, future_ticket, past_payment = self._organizer_then_buyer_setup(
            online_pass_two_tiers, revel_user, organization_owner_user, "cs_scoped_batch"
        )

        stripe_service._cleanup_expired_batch(past_payment)

        future_ticket.refresh_from_db()
        assert future_ticket.status == Ticket.TicketStatus.CANCELLED
        assert not Ticket.objects.filter(pk=past_payment.ticket_id).exists()
        assert Payment.objects.filter(stripe_session_id="cs_scoped_batch", status=Payment.PaymentStatus.FAILED).exists()
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0

    def test_release_batch_tier_capacity_floors_at_zero(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        """count can exceed quantity_sold; the decrement must floor at zero instead of
        blowing the PositiveIntegerField CHECK constraint."""
        _, tiers = online_pass_two_tiers
        tier = tiers[0]
        tier.quantity_sold = 1
        tier.save(update_fields=["quantity_sold"])
        tickets = [
            Ticket.objects.create(
                event=tier.event,
                tier=tier,
                user=revel_user,
                status=Ticket.TicketStatus.PENDING,
                guest_name=f"Guest {i}",
            )
            for i in range(2)
        ]

        stripe_service._release_batch_tier_capacity([ticket.id for ticket in tickets])

        tier.refresh_from_db()
        assert tier.quantity_sold == 0


class TestPaymentIntentCanceledWebhook:
    def test_canceled_intent_cancels_pass_and_restores_pass_counter(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_intent")
        Payment.objects.filter(stripe_session_id="cs_expiry_intent").update(stripe_payment_intent_id="pi_expiry_intent")

        event = _canceled_intent_event("pi_expiry_intent")
        StripeEventHandler(event).handle_payment_intent_canceled(event)

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        for payment in Payment.objects.filter(stripe_session_id="cs_expiry_intent"):
            assert payment.status == Payment.PaymentStatus.FAILED
        for ticket in Ticket.objects.filter(held_pass=held_pass):
            assert ticket.status == Ticket.TicketStatus.CANCELLED


def _reserve_unsessioned(series_pass: SeriesPass, user: RevelUser) -> HeldSeriesPass:
    """Drive the real ONLINE purchase flow only through the reserve step (#632).

    ``SeriesPassPurchaseService.purchase()`` calls ``reserve_series_pass_payments``,
    which makes no Stripe call — the returned held pass and its Payment rows are
    left un-sessioned (``stripe_session_id == ""``), reproducing an abandoned
    pre-session reserve.
    """
    held_pass, _reservation_id = SeriesPassPurchaseService(series_pass, user).purchase()  # type: ignore[misc]
    return HeldSeriesPass.objects.get(pk=held_pass.pk)


class TestUnsessionedReserveReclaim:
    """#632: a reserved-but-not-sessioned pass has held_pass.stripe_session_id=""
    and its Payment rows' stripe_session_id="" too. expire_stranded_held_passes
    (session-keyed) can't find it, which used to strand it PENDING and 409-lock the
    buyer out of re-purchasing via _has_active_held_pass. Every pre-session reclaim
    route must find and release it via its tickets instead.
    """

    def test_beat_task_reclaims_unsessioned_reserve_and_unblocks_repurchase(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _reserve_unsessioned(series_pass, revel_user)
        assert held_pass.stripe_session_id == ""
        payments = Payment.objects.filter(ticket__held_pass=held_pass)
        assert payments.count() == 2
        assert all(p.stripe_session_id == "" for p in payments)

        payments.update(expires_at=timezone.now() - timedelta(minutes=1))

        cleaned = cleanup_expired_payments()
        assert cleaned == 2

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        assert not Ticket.objects.filter(held_pass=held_pass, status=Ticket.TicketStatus.PENDING).exists()

        # The buyer is no longer blocked by the conditional unique constraint / _has_active_held_pass.
        service = SeriesPassPurchaseService(series_pass, revel_user)
        assert service._has_active_held_pass() is False
        new_held_pass, _rid = service.purchase()  # type: ignore[misc]
        assert new_held_pass.pk != held_pass.pk
        assert new_held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.PENDING

    def test_cancel_pending_checkout_reclaims_unsessioned_reserve(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _reserve_unsessioned(series_pass, revel_user)
        payment = Payment.objects.filter(ticket__held_pass=held_pass).select_related("ticket__tier").first()
        assert payment is not None
        assert payment.stripe_session_id == ""

        cancelled = stripe_service.cancel_pending_checkout(str(payment.id), revel_user)
        assert cancelled == 2

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        assert not Ticket.objects.filter(held_pass=held_pass).exists()

        assert SeriesPassPurchaseService(series_pass, revel_user)._has_active_held_pass() is False

    def test_cleanup_expired_batch_reclaims_unsessioned_reserve(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _reserve_unsessioned(series_pass, revel_user)
        payment = Payment.objects.filter(ticket__held_pass=held_pass).select_related("ticket__tier").first()
        assert payment is not None

        stripe_service._cleanup_expired_batch(payment)

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
