"""Tests for the per-event financials projection (#551 addendum)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Payment, Ticket, TicketTier
from events.service.revenue_aggregation import ReportScope, event_financials

pytestmark = pytest.mark.django_db

ALL_TIME = (dt.date.min, dt.date(2999, 12, 31))


def _online(user: RevelUser, event: Event, tier: TicketTier, amount: str, **kw: object) -> Payment:
    ticket = Ticket.objects.create(guest_name="g", user=user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE)
    defaults: dict[str, object] = {
        "ticket": ticket, "user": user, "stripe_session_id": "s", "amount": Decimal(amount),
        "platform_fee": Decimal("0.50"), "currency": "EUR", "status": Payment.PaymentStatus.SUCCEEDED,
    }
    defaults.update(kw)
    return Payment.objects.create(**defaults)


def test_event_financials_gross_is_pre_refund(
    event: Event, event_ticket_tier: TicketTier, public_user: RevelUser, member_user: RevelUser
) -> None:
    """gross stays the full charged amount; refunds reported separately; net = gross - refunds."""
    _online(public_user, event, event_ticket_tier, "20.00")
    _online(
        member_user, event, event_ticket_tier, "10.00",
        status=Payment.PaymentStatus.REFUNDED, refund_amount=Decimal("4.00"),
        refund_status=Payment.RefundStatus.SUCCEEDED, refunded_at=timezone.now(),
    )
    scope = ReportScope(org=event.organization, event_id=event.id, date_from=ALL_TIME[0], date_to=ALL_TIME[1])
    fin = event_financials(event, scope)
    eur = next(c for c in fin.by_currency if c.currency == "EUR")
    assert eur.gross == Decimal("30.00")
    assert eur.refunds == Decimal("4.00")
    assert eur.net == Decimal("26.00")
    assert eur.sold_count == 2
    assert eur.refunded_count == 1


def test_event_financials_empty(event: Event) -> None:
    scope = ReportScope(org=event.organization, event_id=event.id, date_from=ALL_TIME[0], date_to=ALL_TIME[1])
    assert event_financials(event, scope).by_currency == []
