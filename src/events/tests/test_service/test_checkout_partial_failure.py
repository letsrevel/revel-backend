"""Partial-failure integration tests for the reserve/session checkout split (#632).

Two dangerous windows in the split:

- Window A: the reservation step (``BatchTicketService.create_batch`` for an online
  tier) completes -- PENDING tickets + Payment rows exist and ``quantity_sold`` is
  incremented -- but the buyer never calls ``create_batch_session`` (e.g. they close
  the tab). The beat task ``cleanup_expired_payments`` must reclaim the reservation:
  delete the stale Payment/Ticket rows and give the capacity back to the tier.
- Window B: ``create_batch_session`` calls Stripe successfully but fails to persist
  (stamp) the ``stripe_session_id`` onto the Payment rows before returning. Because
  the Payment rows were created by ``reserve_batch_payments`` *before* any Stripe
  call, a "paid session with zero Payment rows" is structurally unreachable -- the
  webhook can always find rows to reconcile against. A retry (idempotent via
  ``idempotency_key=reservation_id``) must recover and stamp successfully.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock
from uuid import UUID

import pytest
from django.db.models.query import QuerySet
from django.utils import timezone
from freezegun import freeze_time
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
from events.schema import TicketPurchaseItem
from events.service import stripe_service
from events.service.batch_ticket_service import BatchTicketService
from events.service.series_pass_purchase import SeriesPassPurchaseService
from events.tasks.payments import cleanup_expired_payments

pytestmark = pytest.mark.django_db


@pytest.fixture
def online_tier(event: Event, organization: Organization) -> TicketTier:
    """An online (Stripe) ticket tier on a Stripe-connected organization."""
    organization.stripe_account_id = "acct_test123"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("3.00")
    organization.platform_fee_fixed = Decimal("0.50")
    organization.save()
    return TicketTier.objects.create(
        event=event,
        name="Online Purchase",
        price=Decimal("50.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
        total_quantity=100,
    )


def _reserve_online(event: Event, tier: TicketTier, user: RevelUser) -> tuple[list[Ticket], UUID]:
    """Drive an online reserve through BatchTicketService so quantity_sold really increments."""
    service = BatchTicketService(event, tier, user)
    items = [TicketPurchaseItem(guest_name="Guest 1")]
    with mock.patch("stripe.checkout.Session.create") as create:
        result = service.create_batch(items)
        create.assert_not_called()
    assert isinstance(result, tuple)
    tickets, reservation_id = result
    return tickets, reservation_id


_real_queryset_update = QuerySet.update


def _fail_only_session_stamp(qs: QuerySet, **kwargs: object) -> int:  # type: ignore[type-arg]
    """QuerySet.update stand-in that fails only the session-id stamp.

    The session functions now run an in-flight hold-extension UPDATE before the
    Stripe call (#632), so a blanket ``QuerySet.update`` patch would blow up
    before Stripe is ever reached. Delegate everything except the stamp.
    """
    if "stripe_session_id" in kwargs:
        raise RuntimeError("stamp boom")
    return _real_queryset_update(qs, **kwargs)


@pytest.fixture
def online_series_pass(event_series: EventSeries) -> SeriesPass:
    """An ONLINE series pass covering 2 future events (the minimum ``get_quote``
    requires -- see ``series_pass_service.get_quote``'s ``remaining < 2`` check) on
    a Stripe-connected organization."""
    org = event_series.organization
    org.stripe_account_id = "acct_series_window_b"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.platform_fee_percent = Decimal("3.00")
    org.platform_fee_fixed = Decimal("0.50")
    org.save()

    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Window B Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    for i in range(2):
        pass_event = Event.objects.create(
            organization=org,
            name=f"Pass Event {i}",
            slug=f"pass-event-window-b-{i}",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=100,
            start=timezone.now() + timedelta(days=i + 1),
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=pass_event,
            name=f"Pass Tier {i}",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=pass_event, tier=tier)
    return series_pass


def _reserve_series_online(series_pass: SeriesPass, user: RevelUser) -> tuple[HeldSeriesPass, UUID]:
    """Drive an online series-pass reserve so PENDING tickets + Payment rows exist, no Stripe call."""
    with mock.patch("stripe.checkout.Session.create") as create:
        result = SeriesPassPurchaseService(series_pass, user).purchase()
        create.assert_not_called()
    assert isinstance(result, tuple)
    held_pass, reservation_id = result
    return held_pass, reservation_id


class TestWindowBStampFailureThenRetry:
    """Window B: Stripe succeeds but the stamp UPDATE fails; a retry must recover."""

    def test_payment_rows_exist_before_stripe_and_retry_recovers_after_stamp_failure(
        self, event: Event, online_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """Payment rows are created by reserve_batch_payments, before any Stripe call.

        First create_batch_session attempt: Stripe.create succeeds, but the stamp
        UPDATE (the ``stripe_session_id``-writing call site in
        stripe_service.create_batch_session; the pre-Stripe hold-extension UPDATE
        is left working) is made to raise. No URL is returned, and the Payment
        rows survive untouched (still
        un-sessioned) -- exactly what lets the Stripe webhook reconcile a paid
        session even if the local stamp never happened. Retrying create_batch_session
        (Stripe mocked to return the SAME fake session, proving the idempotency_key
        would dedupe a real double-submit) then succeeds and stamps the rows.
        """
        tickets, rid = _reserve_online(event, online_tier, member_user)

        # Invariant: Payment rows exist BEFORE any Stripe call is made.
        payments_before = list(Payment.objects.filter(reservation_id=rid))
        assert len(payments_before) == len(tickets) == 1
        assert payments_before[0].stripe_session_id == ""
        assert payments_before[0].status == Payment.PaymentStatus.PENDING

        fake_session = mock.Mock(id="cs_window_b", url="https://checkout.stripe.com/c/cs_window_b")

        # Attempt 1: Stripe succeeds, the stamp UPDATE fails (the hold-extension
        # UPDATE before Stripe is left working — see _fail_only_session_stamp).
        with (
            mock.patch("stripe.checkout.Session.create", return_value=fake_session) as create,
            mock.patch("django.db.models.query.QuerySet.update", autospec=True, side_effect=_fail_only_session_stamp),
        ):
            with pytest.raises(RuntimeError, match="stamp boom"):
                stripe_service.create_batch_session(reservation_id=rid)
            create.assert_called_once()

        # No URL was returned (implicit, since it raised) and the rows are untouched.
        payments_after_failure = list(Payment.objects.filter(reservation_id=rid))
        assert len(payments_after_failure) == 1
        assert payments_after_failure[0].stripe_session_id == ""
        assert payments_after_failure[0].status == Payment.PaymentStatus.PENDING

        # Attempt 2 (retry): Stripe mocked to return the SAME session (idempotent),
        # stamp UPDATE now runs for real and succeeds.
        with mock.patch("stripe.checkout.Session.create", return_value=fake_session) as create_retry:
            url = stripe_service.create_batch_session(reservation_id=rid)
            assert create_retry.call_args.kwargs["idempotency_key"] == str(rid)

        assert url == fake_session.url
        stamped = Payment.objects.get(reservation_id=rid)
        assert stamped.stripe_session_id == "cs_window_b"
        assert stamped.status == Payment.PaymentStatus.PENDING


class TestWindowAAbandonedReserveReclaimed:
    """Window A: an online reserve is abandoned (no session ever created); the beat
    task must reclaim the Payment/Ticket rows and give quantity_sold back."""

    def test_expired_reservation_reclaimed_by_cleanup_task(
        self, event: Event, online_tier: TicketTier, member_user: RevelUser
    ) -> None:
        online_tier.refresh_from_db()
        before_sold = online_tier.quantity_sold

        tickets, rid = _reserve_online(event, online_tier, member_user)

        online_tier.refresh_from_db()
        # Driven through create_batch (not reserve_batch_payments alone), so the
        # increment is real -- proving there is real capacity to reclaim.
        assert online_tier.quantity_sold == before_sold + len(tickets)

        ticket_ids = [t.id for t in tickets]
        assert Payment.objects.filter(reservation_id=rid).exists()
        assert Ticket.objects.filter(id__in=ticket_ids, status=Ticket.TicketStatus.PENDING).exists()

        # Force-expire, as if the buyer abandoned the reserve before ever calling
        # create_batch_session.
        Payment.objects.filter(reservation_id=rid).update(expires_at=timezone.now() - timedelta(minutes=1))

        reclaimed = cleanup_expired_payments()

        assert reclaimed == len(tickets)
        assert not Payment.objects.filter(reservation_id=rid).exists()
        assert not Ticket.objects.filter(id__in=ticket_ids).exists()
        online_tier.refresh_from_db()
        assert online_tier.quantity_sold == before_sold


class TestSeriesPassWindowBStampFailureThenRetry:
    """Window B, mirrored for the series-pass session path: Stripe succeeds but the
    stamp UPDATE fails; a retry (idempotent) must recover and stamp both the Payment
    rows and the HeldSeriesPass."""

    def test_payment_rows_exist_before_stripe_and_retry_recovers_after_stamp_failure(
        self, online_series_pass: SeriesPass, member_user: RevelUser
    ) -> None:
        """Payment rows are created by reserve_series_pass_payments, before any Stripe call.

        First create_series_pass_session attempt: Stripe.create succeeds, but the
        stamp UPDATE is made to raise. The whole function is @transaction.atomic, so
        both the Payment.stripe_session_id stamp AND the held_pass.stripe_session_id
        stamp roll back together -- no URL is returned, and everything survives
        untouched (still un-sessioned). Retrying (Stripe mocked to return the SAME
        fake session, proving idempotency_key would dedupe a real double-submit)
        then succeeds and stamps both the Payment rows and the held pass.
        """
        held_pass, rid = _reserve_series_online(online_series_pass, member_user)

        # Invariant: Payment rows exist BEFORE any Stripe call is made.
        payments_before = list(Payment.objects.filter(reservation_id=rid))
        assert len(payments_before) == 2
        assert all(p.stripe_session_id == "" for p in payments_before)
        assert all(p.status == Payment.PaymentStatus.PENDING for p in payments_before)
        assert held_pass.stripe_session_id == ""

        fake_session = mock.Mock(id="cs_series_window_b", url="https://checkout.stripe.com/c/cs_series_window_b")

        # Attempt 1: Stripe succeeds, the stamp UPDATE fails (the hold-extension
        # UPDATE before Stripe is left working — see _fail_only_session_stamp).
        with (
            mock.patch("stripe.checkout.Session.create", return_value=fake_session) as create,
            mock.patch("django.db.models.query.QuerySet.update", autospec=True, side_effect=_fail_only_session_stamp),
        ):
            with pytest.raises(RuntimeError, match="stamp boom"):
                stripe_service.create_series_pass_session(reservation_id=rid)
            create.assert_called_once()

        # No URL was returned (implicit, since it raised) and everything is untouched.
        payments_after_failure = list(Payment.objects.filter(reservation_id=rid))
        assert len(payments_after_failure) == 2
        assert all(p.stripe_session_id == "" for p in payments_after_failure)
        assert all(p.status == Payment.PaymentStatus.PENDING for p in payments_after_failure)
        held_pass.refresh_from_db()
        assert held_pass.stripe_session_id == ""

        # Attempt 2 (retry): Stripe mocked to return the SAME session (idempotent),
        # stamp UPDATE now runs for real and succeeds.
        with mock.patch("stripe.checkout.Session.create", return_value=fake_session) as create_retry:
            url = stripe_service.create_series_pass_session(reservation_id=rid)
            assert create_retry.call_args.kwargs["idempotency_key"] == str(rid)

        assert url == fake_session.url
        stamped = list(Payment.objects.filter(reservation_id=rid))
        assert all(p.stripe_session_id == "cs_series_window_b" for p in stamped)
        assert all(p.status == Payment.PaymentStatus.PENDING for p in stamped)
        held_pass.refresh_from_db()
        assert held_pass.stripe_session_id == "cs_series_window_b"


class TestMidFlightReclaimGuard:
    """The reserve rows can be reclaimed while ``Session.create`` is in flight (a
    user cancel from a second tab, or the expiry sweep) -- the stamp UPDATE then
    matches zero rows. The session functions must detect that via the stamp's
    rowcount, best-effort expire the just-created Stripe session, and 404 instead
    of releasing a payable URL the webhook could never reconcile (#632)."""

    def test_batch_session_404s_and_expires_session_when_reservation_cancelled_mid_flight(
        self, event: Event, online_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """cancel_pending_checkout lands between Session.create and the stamp: the
        stamp matches 0 rows, so no URL is released and the session is expired."""
        _, rid = _reserve_online(event, online_tier, member_user)
        payment = Payment.objects.get(reservation_id=rid)
        fake_session = mock.Mock(id="cs_mid_flight", url="https://checkout.stripe.com/c/cs_mid_flight")

        def cancel_then_return(*args: object, **kwargs: object) -> mock.Mock:
            stripe_service.cancel_pending_checkout(str(payment.id), member_user)
            return fake_session

        with (
            mock.patch("stripe.checkout.Session.create", side_effect=cancel_then_return) as create,
            mock.patch("stripe.checkout.Session.expire") as expire,
        ):
            with pytest.raises(HttpError) as exc:
                stripe_service.create_batch_session(reservation_id=rid)
            create.assert_called_once()

        assert exc.value.status_code == 404
        expire.assert_called_once()
        assert expire.call_args.args[0] == "cs_mid_flight"
        assert expire.call_args.kwargs.get("stripe_account") == "acct_test123"
        # No row ever carries the orphaned session id. (Single-connection caveat:
        # the in-test cancel is nested inside the session function's atomic block,
        # so the 404's savepoint rollback resurrects the deleted rows here — in
        # production the cancel commits independently and the rows stay gone.)
        assert not Payment.objects.filter(stripe_session_id="cs_mid_flight").exists()

    def test_series_session_404s_and_expires_session_when_reservation_cancelled_mid_flight(
        self, online_series_pass: SeriesPass, member_user: RevelUser
    ) -> None:
        """Series-pass variant: the held pass was already cancelled by the reclaim,
        so it must not be stamped with the orphaned session id either."""
        held_pass, rid = _reserve_series_online(online_series_pass, member_user)
        a_payment = Payment.objects.filter(reservation_id=rid).first()
        assert a_payment is not None
        fake_session = mock.Mock(id="cs_series_mid_flight", url="https://checkout.stripe.com/c/cs_series_mid_flight")

        def cancel_then_return(*args: object, **kwargs: object) -> mock.Mock:
            stripe_service.cancel_pending_checkout(str(a_payment.id), member_user)
            return fake_session

        with (
            mock.patch("stripe.checkout.Session.create", side_effect=cancel_then_return) as create,
            mock.patch("stripe.checkout.Session.expire") as expire,
        ):
            with pytest.raises(HttpError) as exc:
                stripe_service.create_series_pass_session(reservation_id=rid)
            create.assert_called_once()

        assert exc.value.status_code == 404
        expire.assert_called_once()
        assert expire.call_args.args[0] == "cs_series_mid_flight"
        # Neither the payments nor the pass ever carry the orphaned session id.
        # (Single-connection caveat: the in-test cancel is nested inside the
        # session function's atomic block, so the 404's savepoint rollback
        # resurrects the reclaimed rows here — in production the cancel commits
        # independently and they stay gone/CANCELLED.)
        assert not Payment.objects.filter(stripe_session_id="cs_series_mid_flight").exists()
        held_pass.refresh_from_db()
        assert held_pass.stripe_session_id == ""

    def test_series_session_404s_when_pass_cancelled_mid_flight_but_payments_survive(
        self, online_series_pass: SeriesPass, member_user: RevelUser
    ) -> None:
        """Defense-in-depth: the pass flips to CANCELLED mid-call while the Payment
        rows are somehow still PENDING. The conditional pass stamp matches 0 rows;
        the whole stamp (payments included) rolls back and no URL is released."""
        held_pass, rid = _reserve_series_online(online_series_pass, member_user)
        fake_session = mock.Mock(id="cs_pass_cancelled", url="https://checkout.stripe.com/c/cs_pass_cancelled")

        def cancel_pass_then_return(*args: object, **kwargs: object) -> mock.Mock:
            HeldSeriesPass.objects.filter(pk=held_pass.pk).update(status=HeldSeriesPass.HeldSeriesPassStatus.CANCELLED)
            return fake_session

        with (
            mock.patch("stripe.checkout.Session.create", side_effect=cancel_pass_then_return),
            mock.patch("stripe.checkout.Session.expire") as expire,
        ):
            with pytest.raises(HttpError) as exc:
                stripe_service.create_series_pass_session(reservation_id=rid)

        assert exc.value.status_code == 404
        expire.assert_called_once()
        assert expire.call_args.args[0] == "cs_pass_cancelled"
        # The payment stamp rolled back with the raise: rows survive un-sessioned,
        # so the reservation stays reclaimable by the expiry sweep.
        payments = list(Payment.objects.filter(reservation_id=rid))
        assert len(payments) == 2
        assert all(p.stripe_session_id == "" for p in payments)
        held_pass.refresh_from_db()
        assert held_pass.stripe_session_id == ""

    def test_in_flight_hold_extension_prevents_beat_reclaim_mid_call(
        self, event: Event, online_tier: TicketTier, member_user: RevelUser
    ) -> None:
        """The session step atomically extends the reservation hold before calling
        Stripe, so a cleanup_expired_payments run during the (slow) call cannot
        reclaim the rows out from under the stamp (#632)."""
        with freeze_time("2026-07-14 12:00:00") as frozen:
            _, rid = _reserve_online(event, online_tier, member_user)
            # The buyer clicks through 1 minute before the reservation expires.
            Payment.objects.filter(reservation_id=rid).update(expires_at=timezone.now() + timedelta(minutes=1))
            fake_session = mock.Mock(id="cs_slow_stripe", url="https://checkout.stripe.com/c/cs_slow_stripe")

            def slow_stripe_call(*args: object, **kwargs: object) -> mock.Mock:
                # The Stripe round-trip straddles the original expiry, and the
                # beat task fires right in that window.
                frozen.move_to("2026-07-14 12:02:00")
                cleanup_expired_payments()
                return fake_session

            with mock.patch("stripe.checkout.Session.create", side_effect=slow_stripe_call):
                url = stripe_service.create_batch_session(reservation_id=rid)

        assert url == fake_session.url
        stamped = Payment.objects.get(reservation_id=rid)
        assert stamped.stripe_session_id == "cs_slow_stripe"
        assert stamped.status == Payment.PaymentStatus.PENDING


class TestCrossTypeReservationGuard:
    """A reservation for one checkout type must be rejected by the other type's
    session endpoint (#632): ownership alone isn't enough to guarantee the caller
    hit the matching session function, and mixing the two would either strand a
    HeldSeriesPass (batch session on a series reservation) or crash on a None
    held_pass (series session on a batch reservation)."""

    def test_series_reservation_rejected_by_batch_session(
        self, online_series_pass: SeriesPass, member_user: RevelUser
    ) -> None:
        _, rid = _reserve_series_online(online_series_pass, member_user)

        with mock.patch("stripe.checkout.Session.create") as create:
            with pytest.raises(HttpError) as exc:
                stripe_service.create_batch_session(reservation_id=rid)
            create.assert_not_called()
        assert exc.value.status_code == 404

    def test_batch_reservation_rejected_by_series_session(
        self, event: Event, online_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _, rid = _reserve_online(event, online_tier, member_user)

        with mock.patch("stripe.checkout.Session.create") as create:
            with pytest.raises(HttpError) as exc:
                stripe_service.create_series_pass_session(reservation_id=rid)
            create.assert_not_called()
        assert exc.value.status_code == 404
