"""A payment that completes after its ticket was cancelled is refunded, not confirmed (#1459)."""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.utils import timezone

from events.models import Payment, Refund, Ticket, TicketTier
from events.service.stripe_webhooks import StripeEventHandler
from events.tasks.payments import requeue_late_payment_refunds
from events.tasks.refunds import refund_late_payment
from events.tests.test_series_pass.test_backfill import _completed_checkout_event
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db

_SESSION = "cs_late_1459"


@pytest.fixture
def stripe_refund() -> t.Iterator[MagicMock]:
    with patch("stripe.Refund.create") as mock_create:
        mock_create.return_value.id = "re_late"
        yield mock_create


@pytest.fixture
def cancelled_ticket_payment(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    tier_factory: t.Callable[..., TicketTier],
) -> Payment:
    """A PENDING online payment whose ticket was cancelled before the checkout completed."""
    ticket = ticket_factory(tier=tier_factory(), status=Ticket.TicketStatus.CANCELLED)
    return payment_factory(
        ticket, status=Payment.PaymentStatus.PENDING, stripe_session_id=_SESSION, stripe_payment_intent_id=None
    )


def _mark_due(payment: Payment, *, minutes_ago: int = 0) -> None:
    Payment.objects.filter(pk=payment.pk).update(
        status=Payment.PaymentStatus.SUCCEEDED,
        refund_status=Payment.RefundStatus.PENDING,
        stripe_payment_intent_id=f"pi_{payment.pk}",
        updated_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


class TestWebhookMarksTheLatePayment:
    def test_the_payment_gets_the_refund_due_marker_and_no_confirmation(
        self, cancelled_ticket_payment: Payment, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        event = _completed_checkout_event(_SESSION)
        with (
            patch("events.tasks.refunds.refund_late_payment.delay") as dispatch,
            patch("notifications.signals.payment._send_payment_confirmation") as confirmation,
        ):
            with django_capture_on_commit_callbacks(execute=True):
                StripeEventHandler(event).handle_checkout_session_completed(event)

        payment = Payment.objects.get(pk=cancelled_ticket_payment.pk)
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.refund_status == Payment.RefundStatus.PENDING
        assert payment.stripe_payment_intent_id == f"pi_{_SESSION}"
        assert Ticket.objects.get(pk=payment.ticket_id).status == Ticket.TicketStatus.CANCELLED
        dispatch.assert_called_once_with(str(payment.pk))
        confirmation.assert_not_called()


class TestRefundLatePaymentTask:
    def test_refunds_the_full_amount_as_a_late_payment(
        self, cancelled_ticket_payment: Payment, stripe_refund: MagicMock
    ) -> None:
        _mark_due(cancelled_ticket_payment)

        refund_late_payment(str(cancelled_ticket_payment.pk))

        refund = Refund.objects.get(payment=cancelled_ticket_payment)
        assert refund.source == Refund.Source.LATE_PAYMENT
        assert refund.status == Refund.RefundStatus.PENDING
        assert refund.amount == cancelled_ticket_payment.amount
        assert stripe_refund.call_args.kwargs["payment_intent"] == f"pi_{cancelled_ticket_payment.pk}"

    def test_a_second_run_refunds_nothing_more(
        self, cancelled_ticket_payment: Payment, stripe_refund: MagicMock
    ) -> None:
        _mark_due(cancelled_ticket_payment)

        refund_late_payment(str(cancelled_ticket_payment.pk))
        refund_late_payment(str(cancelled_ticket_payment.pk))

        assert stripe_refund.call_count == 1
        assert Refund.objects.filter(payment=cancelled_ticket_payment).count() == 1

    def test_a_ticket_that_is_not_cancelled_is_not_refunded(
        self, cancelled_ticket_payment: Payment, stripe_refund: MagicMock
    ) -> None:
        _mark_due(cancelled_ticket_payment)
        Ticket.objects.filter(pk=cancelled_ticket_payment.ticket_id).update(status=Ticket.TicketStatus.ACTIVE)

        refund_late_payment(str(cancelled_ticket_payment.pk))

        stripe_refund.assert_not_called()

    def test_a_stripe_failure_is_recorded_as_failed(
        self, cancelled_ticket_payment: Payment, stripe_refund: MagicMock
    ) -> None:
        _mark_due(cancelled_ticket_payment)
        stripe_refund.side_effect = stripe.StripeError("card network down")

        with patch("events.tasks.refunds.logger") as log:
            refund_late_payment(str(cancelled_ticket_payment.pk))

        refund = Refund.objects.get(payment=cancelled_ticket_payment)
        assert refund.status == Refund.RefundStatus.FAILED
        assert refund.source == Refund.Source.LATE_PAYMENT
        payment = Payment.objects.get(pk=cancelled_ticket_payment.pk)
        assert payment.refund_status == Payment.RefundStatus.FAILED
        assert log.error.call_args.args[0] == "late_payment_refund_failed"


class TestSweepRequeuesLostDispatches:
    def test_an_old_due_payment_is_requeued(self, cancelled_ticket_payment: Payment) -> None:
        _mark_due(cancelled_ticket_payment, minutes_ago=11)

        with patch("events.tasks.refunds.refund_late_payment.delay") as dispatch:
            assert requeue_late_payment_refunds() == 1

        dispatch.assert_called_once_with(str(cancelled_ticket_payment.pk))

    def test_a_fresh_due_payment_waits_for_its_own_dispatch(self, cancelled_ticket_payment: Payment) -> None:
        _mark_due(cancelled_ticket_payment, minutes_ago=2)

        with patch("events.tasks.refunds.refund_late_payment.delay") as dispatch:
            assert requeue_late_payment_refunds() == 0

        dispatch.assert_not_called()

    def test_a_refund_in_flight_is_not_requeued(self, cancelled_ticket_payment: Payment) -> None:
        _mark_due(cancelled_ticket_payment, minutes_ago=11)
        Refund.objects.create(
            payment=cancelled_ticket_payment,
            amount=cancelled_ticket_payment.amount,
            currency=cancelled_ticket_payment.currency,
            status=Refund.RefundStatus.PENDING,
            source=Refund.Source.USER_CANCELLATION,
        )

        with patch("events.tasks.refunds.refund_late_payment.delay") as dispatch:
            assert requeue_late_payment_refunds() == 0

        dispatch.assert_not_called()


def test_no_payment_confirmation_for_a_cancelled_ticket(
    cancelled_ticket_payment: Payment, django_capture_on_commit_callbacks: t.Any
) -> None:
    with patch("notifications.signals.payment.notification_requested") as requested:
        with django_capture_on_commit_callbacks(execute=True):
            cancelled_ticket_payment.status = Payment.PaymentStatus.SUCCEEDED
            cancelled_ticket_payment.save(update_fields=["status"])

    sent = [call.kwargs.get("notification_type") for call in requested.send.call_args_list]
    assert NotificationType.PAYMENT_CONFIRMATION not in sent
