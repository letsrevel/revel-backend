"""Unit tests for refund_service.issue_refund and remaining_refundable."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
import stripe
from django.db import transaction
from ninja.errors import HttpError

from events.exceptions import NothingToRefundError, RefundInsufficientBalanceError, StripeRefundFailed
from events.models import Payment, Refund, Ticket, TicketTier
from events.service import refund_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def online_paid_ticket(
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
    payment_factory: t.Callable[..., Payment],
) -> Ticket:
    tier = tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("40.00"))
    ticket = ticket_factory(tier=tier)
    payment_factory(
        ticket=ticket,
        amount=Decimal("40.00"),
        status=Payment.PaymentStatus.SUCCEEDED,
        stripe_payment_intent_id="pi_test",
    )
    return ticket


class TestRemainingRefundable:
    def test_no_refunds_full_amount(self, online_paid_ticket: Ticket) -> None:
        assert refund_service.remaining_refundable(online_paid_ticket.payment) == Decimal("40.00")

    def test_pending_and_succeeded_rows_reduce_remaining(self, online_paid_ticket: Ticket) -> None:
        payment = online_paid_ticket.payment
        Refund.objects.create(
            payment=payment,
            amount=Decimal("10.00"),
            currency="EUR",
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.ORGANIZER_API,
        )
        Refund.objects.create(
            payment=payment,
            amount=Decimal("5.00"),
            currency="EUR",
            status=Refund.RefundStatus.PENDING,
            source=Refund.Source.ORGANIZER_API,
        )
        Refund.objects.create(
            payment=payment,
            amount=Decimal("99.00"),
            currency="EUR",
            status=Refund.RefundStatus.FAILED,
            source=Refund.Source.ORGANIZER_API,
        )
        assert refund_service.remaining_refundable(payment) == Decimal("25.00")


class TestIssueRefund:
    def test_full_refund_default_amount(self, online_paid_ticket: Ticket) -> None:
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_1"
            with transaction.atomic():
                row = refund_service.issue_refund(
                    online_paid_ticket.payment,
                    amount=None,
                    initiated_by=online_paid_ticket.user,
                    reason="goodwill",
                    source=Refund.Source.ORGANIZER_API,
                )
        _, kwargs = mock_create.call_args
        assert kwargs["payment_intent"] == "pi_test"
        assert kwargs["amount"] == 4000
        assert kwargs["idempotency_key"] == f"refund:{online_paid_ticket.payment.pk}:0:40.00"
        assert kwargs["metadata"]["refund_id"] == str(row.pk)
        assert kwargs["metadata"]["ticket_id"] == str(online_paid_ticket.pk)
        row.refresh_from_db()
        assert row.status == Refund.RefundStatus.PENDING  # webhook finalizes
        assert row.stripe_refund_id == "re_1"
        assert row.amount == Decimal("40.00")

    def test_partial_then_partial_sequence_advances_key(self, online_paid_ticket: Ticket) -> None:
        payment = online_paid_ticket.payment
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_a"
            with transaction.atomic():
                refund_service.issue_refund(
                    payment,
                    amount=Decimal("10.00"),
                    initiated_by=None,
                    reason="",
                    source=Refund.Source.ORGANIZER_API,
                )
            mock_create.return_value.id = "re_b"
            with transaction.atomic():
                refund_service.issue_refund(
                    payment,
                    amount=Decimal("10.00"),
                    initiated_by=None,
                    reason="",
                    source=Refund.Source.ORGANIZER_API,
                )
        keys = [c.kwargs["idempotency_key"] for c in mock_create.call_args_list]
        assert keys == [f"refund:{payment.pk}:0:10.00", f"refund:{payment.pk}:1:10.00"]

    def test_failed_row_advances_sequence(self, online_paid_ticket: Ticket) -> None:
        """A persisted FAILED row must advance the idempotency key past Stripe's ~24h error cache."""
        payment = online_paid_ticket.payment
        Refund.objects.create(
            payment=payment,
            amount=Decimal("40.00"),
            currency="EUR",
            status=Refund.RefundStatus.FAILED,
            source=Refund.Source.ORGANIZER_API,
        )
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_retry"
            with transaction.atomic():
                refund_service.issue_refund(
                    payment,
                    amount=None,
                    initiated_by=None,
                    reason="",
                    source=Refund.Source.ORGANIZER_API,
                )
        assert mock_create.call_args.kwargs["idempotency_key"] == f"refund:{payment.pk}:1:40.00"

    def test_over_amount_rejected_400(self, online_paid_ticket: Ticket) -> None:
        with pytest.raises(HttpError), transaction.atomic():
            refund_service.issue_refund(
                online_paid_ticket.payment,
                amount=Decimal("41.00"),
                initiated_by=None,
                reason="",
                source=Refund.Source.ORGANIZER_API,
            )

    def test_fully_refunded_raises_nothing_to_refund(self, online_paid_ticket: Ticket) -> None:
        payment = online_paid_ticket.payment
        Refund.objects.create(
            payment=payment,
            amount=Decimal("40.00"),
            currency="EUR",
            status=Refund.RefundStatus.SUCCEEDED,
            source=Refund.Source.ORGANIZER_API,
        )
        with pytest.raises(NothingToRefundError), transaction.atomic():
            refund_service.issue_refund(
                payment,
                amount=None,
                initiated_by=None,
                reason="",
                source=Refund.Source.ORGANIZER_API,
            )

    def test_offline_payment_raises_nothing_to_refund(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """A genuinely offline payment (no Stripe charge) has nothing to refund.

        The gate is the Payment row's own ``stripe_payment_intent_id``, not the
        ticket tier's ``payment_method`` — see ``test_offline_tier_with_stripe_charge_still_refundable``
        for why the tier alone can't be trusted (series passes).
        """
        tier = tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("40.00"))
        ticket = ticket_factory(tier=tier)
        payment = payment_factory(ticket=ticket, amount=Decimal("40.00"), stripe_payment_intent_id="")
        with pytest.raises(NothingToRefundError), transaction.atomic():
            refund_service.issue_refund(
                payment,
                amount=None,
                initiated_by=None,
                reason="",
                source=Refund.Source.ORGANIZER_API,
            )

    def test_offline_tier_with_stripe_charge_still_refundable(
        self,
        ticket_factory: t.Callable[..., Ticket],
        tier_factory: t.Callable[..., TicketTier],
        payment_factory: t.Callable[..., Payment],
    ) -> None:
        """A series pass paid online may be materialized onto an OFFLINE/FREE tier ticket

        (the tier governs the event's own checkout, not how the pass was charged); the
        Payment row's Stripe fields, not ``tier.payment_method``, must gate the refund.
        """
        tier = tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("40.00"))
        ticket = ticket_factory(tier=tier)
        payment = payment_factory(
            ticket=ticket,
            amount=Decimal("40.00"),
            status=Payment.PaymentStatus.SUCCEEDED,
            stripe_payment_intent_id="pi_pass_test",
        )
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_pass"
            with transaction.atomic():
                row = refund_service.issue_refund(
                    payment,
                    amount=None,
                    initiated_by=None,
                    reason="",
                    source=Refund.Source.ORGANIZER_API,
                )
        assert row.amount == Decimal("40.00")

    def test_balance_insufficient_maps_and_rolls_back(self, online_paid_ticket: Ticket) -> None:
        err = stripe.error.InvalidRequestError(message="insufficient", param=None, code="balance_insufficient")
        with patch("stripe.Refund.create", side_effect=err):
            with pytest.raises(RefundInsufficientBalanceError), transaction.atomic():
                refund_service.issue_refund(
                    online_paid_ticket.payment,
                    amount=None,
                    initiated_by=None,
                    reason="",
                    source=Refund.Source.ORGANIZER_API,
                )
        assert Refund.objects.count() == 0  # PENDING row rolled back

    def test_generic_stripe_error_maps_to_refund_failed(self, online_paid_ticket: Ticket) -> None:
        with patch("stripe.Refund.create", side_effect=stripe.error.APIError("boom")):
            with pytest.raises(StripeRefundFailed), transaction.atomic():
                refund_service.issue_refund(
                    online_paid_ticket.payment,
                    amount=None,
                    initiated_by=None,
                    reason="",
                    source=Refund.Source.ORGANIZER_API,
                )

    def test_connected_account_header(self, online_paid_ticket: Ticket) -> None:
        org = online_paid_ticket.event.organization
        org.stripe_account_id = "acct_connected_123"
        org.save(update_fields=["stripe_account_id"])
        with patch("stripe.Refund.create") as mock_create:
            mock_create.return_value.id = "re_c"
            with transaction.atomic():
                refund_service.issue_refund(
                    online_paid_ticket.payment,
                    amount=None,
                    initiated_by=None,
                    reason="",
                    source=Refund.Source.ORGANIZER_API,
                )
        assert mock_create.call_args.kwargs["stripe_account"] == "acct_connected_123"
