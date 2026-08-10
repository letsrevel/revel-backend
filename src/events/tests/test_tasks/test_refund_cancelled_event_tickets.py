"""Bulk cancel-and-refund sweep: parent fan-out + per-ticket subtask behavior."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
import stripe

from events.models import Event, Payment, Refund, Ticket, TicketTier
from events.models.ticket import CancellationSource
from events.tasks.refunds import (
    _SUMMARY_MAX_ATTEMPTS,
    _SUMMARY_RETRY_COUNTDOWN,
    refund_cancelled_event_tickets,
    refund_one_cancelled_event_ticket,
    send_event_refund_summary_task,
)

pytestmark = pytest.mark.django_db


class TestRefundCancelledEventTicketsFanOut:
    """The parent task only snapshots ticket ids and dispatches — no Stripe calls."""

    def test_dispatches_one_subtask_per_non_cancelled_ticket(
        self,
        event: Event,
        organization_owner_user: t.Any,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
    ) -> None:
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE)
        t1 = ticket_factory(tier=online)
        t2 = ticket_factory(tier=online)
        ticket_factory(tier=online, status=Ticket.TicketStatus.CANCELLED)  # already cancelled, excluded

        with patch("events.tasks.refunds.refund_one_cancelled_event_ticket.delay") as mock_delay:
            summary = refund_cancelled_event_tickets(str(event.id), str(organization_owner_user.id))

        assert summary == {"dispatched": 2}
        assert mock_delay.call_count == 2
        dispatched_ids = {call.args[0] for call in mock_delay.call_args_list}
        assert dispatched_ids == {str(t1.id), str(t2.id)}
        for call in mock_delay.call_args_list:
            assert call.args[1] == str(organization_owner_user.id)

    def test_schedules_summary_task_when_something_was_dispatched(
        self,
        event: Event,
        organization_owner_user: t.Any,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
    ) -> None:
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE)
        ticket_factory(tier=online)

        with (
            patch("events.tasks.refunds.refund_one_cancelled_event_ticket.delay"),
            patch("events.tasks.refunds.send_event_refund_summary_task.apply_async") as mock_apply_async,
        ):
            refund_cancelled_event_tickets(str(event.id), str(organization_owner_user.id))

        mock_apply_async.assert_called_once_with(args=(str(event.id),), countdown=_SUMMARY_RETRY_COUNTDOWN)

    def test_skips_summary_task_when_nothing_was_dispatched(
        self,
        event: Event,
        organization_owner_user: t.Any,
    ) -> None:
        with (
            patch("events.tasks.refunds.refund_one_cancelled_event_ticket.delay") as mock_delay,
            patch("events.tasks.refunds.send_event_refund_summary_task.apply_async") as mock_apply_async,
        ):
            summary = refund_cancelled_event_tickets(str(event.id), str(organization_owner_user.id))

        assert summary == {"dispatched": 0}
        mock_delay.assert_not_called()
        mock_apply_async.assert_not_called()


class TestRefundOneCancelledEventTicket:
    """Behavior coverage for the per-ticket subtask, called directly (not via .delay)."""

    def test_mixed_population(
        self,
        event: Event,
        organization_owner_user: t.Any,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        event.cancellation_reason = "Venue flooded"
        event.save(update_fields=["cancellation_reason"])
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        offline = tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("20.00"))
        t_paid = ticket_factory(tier=online)
        payment_factory(
            ticket=t_paid,
            amount=Decimal("40.00"),
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_payment_intent_id="pi_ok",
        )
        t_pending = ticket_factory(tier=online, status=Ticket.TicketStatus.PENDING)
        payment_factory(ticket=t_pending, amount=Decimal("40.00"), status=Payment.PaymentStatus.PENDING)
        t_offline = ticket_factory(tier=offline)
        t_cancelled = ticket_factory(tier=offline, status=Ticket.TicketStatus.CANCELLED)
        online.quantity_sold = 2
        online.save(update_fields=["quantity_sold"])
        offline.quantity_sold = 1
        offline.save(update_fields=["quantity_sold"])

        with (
            patch("stripe.Refund.create") as mock_create,
            patch("events.service.waitlist_service.enqueue_waitlist_processing") as mock_enqueue,
        ):
            mock_create.return_value.id = "re_bulk"
            for tk in (t_paid, t_pending, t_offline):
                refund_one_cancelled_event_ticket(str(tk.id), str(organization_owner_user.id))

        mock_enqueue.assert_not_called()

        for tk in (t_paid, t_pending, t_offline):
            tk.refresh_from_db()
            assert tk.status == Ticket.TicketStatus.CANCELLED
            assert tk.cancellation_source == "event_cancellation"
            assert tk.cancellation_reason == event.cancellation_reason
            assert tk.cancelled_by_id == organization_owner_user.id
        t_cancelled.refresh_from_db()
        assert t_cancelled.cancelled_at is None  # untouched

        row = Refund.objects.get(payment__ticket=t_paid)
        assert row.source == Refund.Source.EVENT_CANCELLATION
        assert row.amount == Decimal("40.00")
        pending_payment = Payment.objects.get(ticket=t_pending)
        assert pending_payment.status == Payment.PaymentStatus.FAILED

        online.refresh_from_db()
        offline.refresh_from_db()
        assert online.quantity_sold == 0  # 2 - 2 (t_paid, t_pending both online)
        assert offline.quantity_sold == 0  # 1 - 1 (t_offline)

    def test_stripe_failure_records_failed_row_but_cancellation_still_proceeds(
        self,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        t1 = ticket_factory(tier=online)
        payment_factory(
            ticket=t1,
            amount=Decimal("40.00"),
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_payment_intent_id="pi_a",
        )

        with patch("stripe.Refund.create", side_effect=stripe.error.APIError("boom")):
            refund_one_cancelled_event_ticket(str(t1.id), None)

        failed_row = Refund.objects.get(payment__ticket=t1)
        assert failed_row.status == Refund.RefundStatus.FAILED
        assert "boom" in failed_row.failure_reason
        t1.refresh_from_db()
        assert t1.status == Ticket.TicketStatus.CANCELLED  # cancellation proceeds despite the refund failure

    def test_failed_row_advances_idempotency_key_on_rerun(
        self,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """A sweep re-run after a FAILED refund row must issue a NEW idempotency key.

        Stripe caches responses (including 4xx) per idempotency key for ~24h; if the
        key never advanced, re-running the sweep would replay the cached failure
        instead of actually retrying the refund.
        """
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        tk = ticket_factory(tier=online)
        payment_factory(
            ticket=tk,
            amount=Decimal("40.00"),
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_payment_intent_id="pi_retry_key",
        )

        with patch("stripe.Refund.create", side_effect=stripe.error.APIError("boom")) as mock_fail:
            refund_one_cancelled_event_ticket(str(tk.id), None)
        assert Refund.objects.filter(status=Refund.RefundStatus.FAILED).count() == 1

        # Simulate a crash between the FAILED-row write and the cancel transaction:
        # the ticket is still non-CANCELLED when the sweep is re-dispatched.
        Ticket.objects.filter(pk=tk.pk).update(status=Ticket.TicketStatus.ACTIVE)
        with patch("stripe.Refund.create") as mock_ok:
            mock_ok.return_value.id = "re_retry_ok"
            refund_one_cancelled_event_ticket(str(tk.id), None)

        first_key = mock_fail.call_args.kwargs["idempotency_key"]
        second_key = mock_ok.call_args.kwargs["idempotency_key"]
        assert first_key != second_key

    def test_already_cancelled_ticket_is_a_no_op(
        self,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        tk = ticket_factory(tier=online)
        payment_factory(
            ticket=tk,
            amount=Decimal("40.00"),
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_payment_intent_id="pi_r",
        )
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_r"
            refund_one_cancelled_event_ticket(str(tk.id), None)
            # Second call: ticket is now CANCELLED, must be a pure no-op.
            refund_one_cancelled_event_ticket(str(tk.id), None)
        assert Refund.objects.count() == 1
        tk.refresh_from_db()
        assert tk.status == Ticket.TicketStatus.CANCELLED

    def test_dispatches_attendee_visibility_flags_recompute_on_commit(
        self,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """The ``.update()`` cancellation bypass skips the ``post_save`` signal that
        normally recomputes ``Event.attendee_count``/``is_full`` — this task must
        replicate that dispatch explicitly, or a cancelled event's public attendee
        count freezes at its pre-cancellation value forever (un-cancel is blocked)."""
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        tk = ticket_factory(tier=online)
        payment_factory(
            ticket=tk,
            amount=Decimal("40.00"),
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_payment_intent_id="pi_visibility",
        )

        with patch("events.tasks.refunds.build_attendee_visibility_flags.delay") as mock_visibility:
            with patch("stripe.Refund.create") as mock_create:
                mock_create.return_value.id = "re_visibility"
                with django_capture_on_commit_callbacks(execute=True):
                    refund_one_cancelled_event_ticket(str(tk.id), None)

        mock_visibility.assert_called_once_with(str(event.id))

    def test_resumes_after_refund_succeeds_but_cancellation_did_not_commit(
        self,
        event: Event,
        organization_owner_user: t.Any,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """Simulates a crash between the refund txn and the cancel txn (H1).

        The ticket is still non-CANCELLED, but a SUCCEEDED Refund row already covers
        the full amount. Re-entering the subtask must find nothing left to refund
        (no second Stripe call) and proceed straight to cancellation.
        """
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        tk = ticket_factory(tier=online)
        payment = payment_factory(
            ticket=tk,
            amount=Decimal("40.00"),
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_payment_intent_id="pi_crash",
        )
        Refund.objects.create(
            payment=payment,
            amount=Decimal("40.00"),
            currency="EUR",
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.EVENT_CANCELLATION,
        )

        with patch("stripe.Refund.create") as mock_create:
            refund_one_cancelled_event_ticket(str(tk.id), str(organization_owner_user.id))

        mock_create.assert_not_called()
        tk.refresh_from_db()
        assert tk.status == Ticket.TicketStatus.CANCELLED
        assert Refund.objects.filter(payment=payment).count() == 1


class TestSendEventRefundSummaryTask:
    """The summary task polls DB state — there is no synchronous fan-out completion signal."""

    def test_reschedules_while_tickets_remain_and_under_attempt_budget(
        self,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
    ) -> None:
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE)
        ticket_factory(tier=online)  # still ACTIVE — fan-out hasn't finished

        with (
            patch("events.tasks.refunds.send_event_refund_summary_task.apply_async") as mock_apply_async,
            patch("events.tasks.refunds.send_event_refund_summary") as mock_send,
        ):
            send_event_refund_summary_task(str(event.id), attempt=3)

        mock_apply_async.assert_called_once_with(args=(str(event.id), 4), countdown=_SUMMARY_RETRY_COUNTDOWN)
        mock_send.assert_not_called()

    def test_sends_summary_with_correct_counts_once_every_ticket_is_cancelled(
        self,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        t_succeeded = ticket_factory(
            tier=online,
            status=Ticket.TicketStatus.CANCELLED,
            cancellation_source=CancellationSource.EVENT_CANCELLATION,
        )
        payment_succeeded = payment_factory(
            ticket=t_succeeded, amount=Decimal("40.00"), status=Payment.PaymentStatus.REFUNDED
        )
        Refund.objects.create(
            payment=payment_succeeded,
            amount=Decimal("40.00"),
            currency="EUR",
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.EVENT_CANCELLATION,
        )

        t_failed = ticket_factory(
            tier=online,
            status=Ticket.TicketStatus.CANCELLED,
            cancellation_source=CancellationSource.EVENT_CANCELLATION,
        )
        payment_failed = payment_factory(ticket=t_failed, amount=Decimal("40.00"), status=Payment.PaymentStatus.FAILED)
        Refund.objects.create(
            payment=payment_failed,
            amount=Decimal("40.00"),
            currency="EUR",
            status=Refund.RefundStatus.FAILED,
            failure_reason="card declined",
            source=Refund.Source.EVENT_CANCELLATION,
        )

        with (
            patch("events.tasks.refunds.send_event_refund_summary_task.apply_async") as mock_apply_async,
            patch("events.tasks.refunds.send_event_refund_summary") as mock_send,
        ):
            send_event_refund_summary_task(str(event.id))

        mock_apply_async.assert_not_called()
        mock_send.assert_called_once_with(event=event, cancelled=2, refunded=1, failed=1, still_active=0)

    def test_a_retried_and_succeeded_refund_is_not_counted_as_failed(
        self,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """A payment with both a FAILED row and a later SUCCEEDED row is a success, not a failure."""
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
        tk = ticket_factory(
            tier=online,
            status=Ticket.TicketStatus.CANCELLED,
            cancellation_source=CancellationSource.EVENT_CANCELLATION,
        )
        payment = payment_factory(ticket=tk, amount=Decimal("40.00"), status=Payment.PaymentStatus.REFUNDED)
        Refund.objects.create(
            payment=payment,
            amount=Decimal("40.00"),
            currency="EUR",
            status=Refund.RefundStatus.FAILED,
            failure_reason="transient error",
            source=Refund.Source.EVENT_CANCELLATION,
        )
        Refund.objects.create(
            payment=payment,
            amount=Decimal("40.00"),
            currency="EUR",
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.EVENT_CANCELLATION,
        )

        with patch("events.tasks.refunds.send_event_refund_summary") as mock_send:
            send_event_refund_summary_task(str(event.id))

        mock_send.assert_called_once_with(event=event, cancelled=1, refunded=1, failed=0, still_active=0)

    def test_gives_up_after_max_attempts_and_sends_with_still_active_count(
        self,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
    ) -> None:
        online = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE)
        ticket_factory(tier=online)  # never cancelled — simulates a stuck/lost subtask

        with (
            patch("events.tasks.refunds.send_event_refund_summary_task.apply_async") as mock_apply_async,
            patch("events.tasks.refunds.send_event_refund_summary") as mock_send,
        ):
            send_event_refund_summary_task(str(event.id), attempt=_SUMMARY_MAX_ATTEMPTS)

        mock_apply_async.assert_not_called()
        mock_send.assert_called_once_with(event=event, cancelled=0, refunded=0, failed=0, still_active=1)
