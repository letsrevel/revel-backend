"""Refund model behavior."""

import typing as t
from decimal import Decimal

import pytest

from events.models import Payment, Refund, Ticket

pytestmark = pytest.mark.django_db


def test_refund_row_links_to_payment(
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
) -> None:
    ticket = ticket_factory()
    payment = payment_factory(ticket=ticket, amount=Decimal("40.00"))
    refund = Refund.objects.create(
        payment=payment,
        amount=Decimal("10.00"),
        currency="EUR",
        source=Refund.Source.ORGANIZER_API,
    )
    assert refund.status == Refund.RefundStatus.PENDING
    assert list(payment.refunds.all()) == [refund]
