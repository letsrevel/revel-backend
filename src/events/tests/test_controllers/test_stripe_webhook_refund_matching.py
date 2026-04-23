"""Tests for handle_charge_refunded's per-payment matching strategy."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import stripe

from events.models import Payment, Ticket
from events.models.ticket import CancellationSource
from events.service.stripe_webhooks import StripeEventHandler

pytestmark = pytest.mark.django_db


def _charge_event(payment_intent_id: str, refunds: list[dict]) -> stripe.Event:
    ev = MagicMock(spec=stripe.Event)
    ev.type = "charge.refunded"
    ev.data = MagicMock()
    ev.data.object = {
        "id": "ch_test",
        "payment_intent": payment_intent_id,
        "refunds": {"data": refunds},
    }
    # Make dict(event) serializable — tests shouldn't care about exact shape,
    # so patch out raw_response assignment in the handler by providing __iter__.
    ev.__iter__.return_value = iter([])
    return ev


def _batch(payments: list[Payment], intent_id: str) -> None:
    Payment.objects.filter(pk__in=[p.pk for p in payments]).update(
        stripe_payment_intent_id=intent_id, status=Payment.PaymentStatus.SUCCEEDED
    )


class TestChargeRefundedMatching:
    def test_branch_1_existing_stripe_refund_id_match(self, batch_of_4_online_payments: list[Payment]) -> None:
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[1]
        target.stripe_refund_id = "re_already_recorded"
        target.refund_status = Payment.RefundStatus.PENDING
        target.save(update_fields=["stripe_refund_id", "refund_status"])

        refund: dict = {"id": "re_already_recorded", "amount": 4000, "metadata": {}}
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )

        target.refresh_from_db()
        assert target.refund_status == Payment.RefundStatus.SUCCEEDED
        assert target.status == Payment.PaymentStatus.REFUNDED
        for other in payments:
            if other.pk == target.pk:
                continue
            other.refresh_from_db()
            assert other.status == Payment.PaymentStatus.SUCCEEDED, "other payments must be untouched"

    def test_branch_2_metadata_ticket_id_match(self, batch_of_4_online_payments: list[Payment]) -> None:
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[2]
        refund: dict = {
            "id": "re_new",
            "amount": 4000,
            "metadata": {"ticket_id": str(target.ticket_id)},
        }
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        target.refresh_from_db()
        assert target.refund_status == Payment.RefundStatus.SUCCEEDED
        target.ticket.refresh_from_db()
        assert target.ticket.status == Ticket.TicketStatus.CANCELLED
        assert target.ticket.cancellation_source == CancellationSource.STRIPE_DASHBOARD
        for other in payments:
            if other.pk == target.pk:
                continue
            other.refresh_from_db()
            assert other.status == Payment.PaymentStatus.SUCCEEDED

    def test_branch_3_exact_amount_unambiguous(self, batch_of_4_online_payments: list[Payment]) -> None:
        payments = batch_of_4_online_payments
        # Mix amounts so exactly one matches.
        payments[0].amount = Decimal("50.00")
        payments[0].save(update_fields=["amount"])
        _batch(payments, "pi_batch")

        refund: dict = {"id": "re_new", "amount": 5000, "metadata": {}}
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        payments[0].refresh_from_db()
        assert payments[0].refund_status == Payment.RefundStatus.SUCCEEDED

    def test_branch_4_full_intent_refund_applies_to_all(self, batch_of_4_online_payments: list[Payment]) -> None:
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        total_cents = int(sum(p.amount for p in payments) * 100)
        refund: dict = {"id": "re_full", "amount": total_cents, "metadata": {}}
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        for p in payments:
            p.refresh_from_db()
            assert p.refund_status == Payment.RefundStatus.SUCCEEDED
            assert p.status == Payment.PaymentStatus.REFUNDED
            p.ticket.refresh_from_db()
            assert p.ticket.status == Ticket.TicketStatus.CANCELLED

    def test_branch_5_ambiguous_logged_no_mutation(
        self, batch_of_4_online_payments: list[Payment], caplog: pytest.LogCaptureFixture
    ) -> None:
        payments = batch_of_4_online_payments
        # All payments cost the same AND amount doesn't equal full intent.
        _batch(payments, "pi_batch")
        refund: dict = {"id": "re_ambig", "amount": 4000, "metadata": {}}  # matches any single payment
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        for p in payments:
            p.refresh_from_db()
            assert p.status == Payment.PaymentStatus.SUCCEEDED
            assert p.refund_status is None

    def test_duplicate_webhook_is_idempotent(self, batch_of_4_online_payments: list[Payment]) -> None:
        payments = batch_of_4_online_payments
        _batch(payments, "pi_batch")
        target = payments[0]
        target.stripe_refund_id = "re_a"
        target.refund_status = Payment.RefundStatus.SUCCEEDED
        target.status = Payment.PaymentStatus.REFUNDED
        target.save(update_fields=["stripe_refund_id", "refund_status", "status"])
        refund: dict = {"id": "re_a", "amount": int(target.amount * 100), "metadata": {}}
        # Replay — should be a no-op.
        StripeEventHandler(_charge_event("pi_batch", [refund])).handle_charge_refunded(
            _charge_event("pi_batch", [refund])
        )
        target.refresh_from_db()
        assert target.status == Payment.PaymentStatus.REFUNDED
