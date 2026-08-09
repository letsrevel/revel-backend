"""Bulk cancel-and-refund sweep."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
import stripe

from events.models import Payment, Refund, Ticket, TicketTier
from events.tasks.refunds import refund_cancelled_event_tickets

pytestmark = pytest.mark.django_db


def test_mixed_population_sweep(
    event: t.Any,
    organization_owner_user: t.Any,
    ticket_factory: t.Callable[..., Ticket],
    tier_factory: t.Callable[..., TicketTier],
    payment_factory: t.Callable[..., Payment],
) -> None:
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

    with patch("stripe.Refund.create") as mock_create:
        mock_create.return_value.id = "re_bulk"
        summary = refund_cancelled_event_tickets(str(event.id), str(organization_owner_user.id))

    for tk in (t_paid, t_pending, t_offline):
        tk.refresh_from_db()
        assert tk.status == Ticket.TicketStatus.CANCELLED
        assert tk.cancellation_source == "event_cancellation"
    t_cancelled.refresh_from_db()
    assert t_cancelled.cancelled_at is None  # untouched

    row = Refund.objects.get(payment__ticket=t_paid)
    assert row.source == Refund.Source.EVENT_CANCELLATION
    assert row.amount == Decimal("40.00")
    pending_payment = Payment.objects.get(ticket=t_pending)
    assert pending_payment.status == Payment.PaymentStatus.FAILED
    assert summary == {"cancelled": 3, "refunded": 1, "failed": 0}


def test_stripe_failure_records_failed_row_and_continues(
    event: t.Any,
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
    t2 = ticket_factory(tier=online)
    payment_factory(
        ticket=t2,
        amount=Decimal("40.00"),
        status=Payment.PaymentStatus.SUCCEEDED,
        stripe_payment_intent_id="pi_b",
    )

    def _fail_first(**kwargs: t.Any) -> t.Any:
        if kwargs["payment_intent"] == "pi_a":
            raise stripe.error.APIError("boom")
        return t.cast(t.Any, type("R", (), {"id": "re_ok"}))

    with patch("stripe.Refund.create", side_effect=_fail_first):
        summary = refund_cancelled_event_tickets(str(event.id), None)

    assert summary["failed"] == 1
    assert summary["refunded"] == 1
    failed_row = Refund.objects.get(payment__ticket=t1)
    assert failed_row.status == Refund.RefundStatus.FAILED
    assert "boom" in failed_row.failure_reason
    t1.refresh_from_db()
    assert t1.status == Ticket.TicketStatus.CANCELLED  # cancellation survives the refund failure


def test_task_is_reentrant(
    event: t.Any,
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
        refund_cancelled_event_tickets(str(event.id), None)
        summary2 = refund_cancelled_event_tickets(str(event.id), None)
    assert summary2 == {"cancelled": 0, "refunded": 0, "failed": 0}
    assert Refund.objects.count() == 1
