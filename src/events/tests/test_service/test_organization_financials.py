"""Tests for the organization financials projection (#551 addendum)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service.revenue_aggregation import ReportScope, organization_financials

pytestmark = pytest.mark.django_db

ALL_TIME = (dt.date.min, dt.date(2999, 12, 31))


def _online(user: RevelUser, event: Event, tier: TicketTier, amount: str, currency: str = "EUR") -> Payment:
    ticket = Ticket.objects.create(guest_name="g", user=user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE)
    return Payment.objects.create(
        ticket=ticket, user=user, stripe_session_id="s", amount=Decimal(amount),
        platform_fee=Decimal("0.50"), currency=currency, status=Payment.PaymentStatus.SUCCEEDED,
    )


def _second_online_event(org: Organization, slug: str, price: str) -> tuple[Event, TicketTier]:
    """Create an extra ONLINE event+tier in the same org (no event factory exists)."""
    now = timezone.now()
    ev = Event.objects.create(organization=org, name=slug, slug=slug, start=now, end=now + dt.timedelta(hours=2))
    tier = TicketTier.objects.create(
        event=ev, name="GA", price=Decimal(price), currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    return ev, tier


def _scope(org: Organization) -> ReportScope:
    return ReportScope(org=org, event_id=None, date_from=ALL_TIME[0], date_to=ALL_TIME[1])


def test_org_financials_sorted_by_revenue(
    organization: Organization, event: Event, event_ticket_tier: TicketTier,
    public_user: RevelUser, member_user: RevelUser,
) -> None:
    """Events order by net within the active currency; order flips with `order`."""
    _online(public_user, event, event_ticket_tier, "100.00")  # the fixture event = bigger
    small, small_tier = _second_online_event(organization, "small-fin", "10.00")
    _online(member_user, small, small_tier, "10.00")

    desc = organization_financials(_scope(organization), currency=None, sort="revenue", order="desc")
    assert desc.active_currency == "EUR"
    assert desc.available_currencies == ["EUR"]
    assert [e.event_id for e in desc.events] == [event.id, small.id]

    asc = organization_financials(_scope(organization), currency=None, sort="revenue", order="asc")
    assert [e.event_id for e in asc.events] == [small.id, event.id]


def test_org_financials_dominant_currency_and_filter(
    organization: Organization, event: Event, event_ticket_tier: TicketTier,
    public_user: RevelUser, member_user: RevelUser
) -> None:
    """Dominant currency = highest gross; ?currency= scopes totals/events to it."""
    _online(public_user, event, event_ticket_tier, "100.00", currency="EUR")
    _online(member_user, event, event_ticket_tier, "5.00", currency="USD")
    fin = organization_financials(_scope(organization), currency=None, sort="revenue", order="desc")
    assert fin.active_currency == "EUR"
    assert set(fin.available_currencies) == {"EUR", "USD"}
    filtered = organization_financials(_scope(organization), currency="USD", sort="revenue", order="desc")
    assert filtered.active_currency == "USD"
    assert all(c.currency == "USD" for e in filtered.events for c in e.by_currency)
    assert [t.currency for t in filtered.totals] == ["USD"]


def test_org_financials_empty_period(organization: Organization) -> None:
    fin = organization_financials(_scope(organization), currency=None, sort="revenue", order="desc")
    assert fin.events == []
    assert fin.totals == []
    assert fin.available_currencies == []
    assert fin.active_currency is None
