"""Tests for the organization financials projection (#551 addendum)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    Payment,
    Ticket,
    TicketTier,
)
from events.service.revenue_aggregation import ReportScope, organization_financials

pytestmark = pytest.mark.django_db

ALL_TIME = (dt.date.min, dt.date(2999, 12, 31))


def _online(user: RevelUser, event: Event, tier: TicketTier, amount: str, currency: str = "EUR") -> Payment:
    ticket = Ticket.objects.create(guest_name="g", user=user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE)
    return Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id="s",
        amount=Decimal(amount),
        platform_fee=Decimal("0.50"),
        currency=currency,
        status=Payment.PaymentStatus.SUCCEEDED,
    )


def _second_online_event(org: Organization, slug: str, price: str) -> tuple[Event, TicketTier]:
    """Create an extra ONLINE event+tier in the same org (no event factory exists)."""
    now = timezone.now()
    ev = Event.objects.create(organization=org, name=slug, slug=slug, start=now, end=now + dt.timedelta(hours=2))
    tier = TicketTier.objects.create(
        event=ev,
        name="GA",
        price=Decimal(price),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    return ev, tier


def _scope(org: Organization) -> ReportScope:
    return ReportScope(org=org, event_id=None, date_from=ALL_TIME[0], date_to=ALL_TIME[1])


def test_org_financials_sorted_by_revenue(
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    public_user: RevelUser,
    member_user: RevelUser,
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
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    public_user: RevelUser,
    member_user: RevelUser,
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
    assert fin.memberships == []
    assert fin.combined_totals == []
    assert fin.available_currencies == []
    assert fin.active_currency is None


def _membership_payment(
    organization: Organization,
    user: RevelUser,
    amount: str,
    *,
    currency: str = "EUR",
    platform_fee: str = "0.00",
    refund_amount: str | None = None,
    plan_name: str = "Monthly",
) -> MembershipPayment:
    """Create a settled membership payment (with its plan + subscription) for the org."""
    tier = MembershipTier.objects.get(organization=organization, name="General membership")
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier, name=plan_name, price=Decimal(amount), currency=currency, period_unit="month"
    )
    now = timezone.now()
    subscription = MembershipSubscription.objects.create(
        organization=organization,
        user=user,
        plan=plan,
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_start=now,
        current_period_end=now + dt.timedelta(days=30),
    )
    return MembershipPayment.objects.create(
        subscription=subscription,
        amount=Decimal(amount),
        currency=currency,
        status=MembershipPayment.PaymentStatus.SUCCEEDED,
        period_start=now,
        period_end=now + dt.timedelta(days=30),
        platform_fee=Decimal(platform_fee),
        refund_amount=Decimal(refund_amount) if refund_amount is not None else None,
        refunded_at=now if refund_amount is not None else None,
    )


def test_org_financials_reports_memberships_beside_tickets(
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    public_user: RevelUser,
    member_user: RevelUser,
) -> None:
    """Membership money lands in its own block and in the combined totals, never in ticket totals."""
    _online(public_user, event, event_ticket_tier, "100.00")
    _membership_payment(organization, member_user, "30.00", platform_fee="1.50")

    fin = organization_financials(_scope(organization), currency=None, sort="revenue", order="desc")

    assert [t.gross for t in fin.totals] == [Decimal("100.00")]  # tickets untouched
    assert len(fin.memberships) == 1
    memberships = fin.memberships[0]
    assert memberships.currency == "EUR"
    assert memberships.gross == Decimal("30.00")
    assert memberships.platform_fee == Decimal("1.50")
    assert memberships.net == Decimal("30.00")
    assert memberships.payment_count == 1
    assert memberships.refunded_amount == Decimal("0.00")

    assert len(fin.combined_totals) == 1
    combined = fin.combined_totals[0]
    assert (combined.tickets_net, combined.memberships_net, combined.net) == (
        Decimal("100.00"),
        Decimal("30.00"),
        Decimal("130.00"),
    )


def test_org_financials_membership_only_org(organization: Organization, member_user: RevelUser) -> None:
    """An org that sells nothing but memberships still reports money and a currency."""
    _membership_payment(organization, member_user, "12.00", currency="USD")

    fin = organization_financials(_scope(organization), currency=None, sort="revenue", order="desc")

    assert fin.totals == []
    assert fin.events == []
    assert fin.available_currencies == ["USD"]
    assert fin.active_currency == "USD"
    assert [m.gross for m in fin.memberships] == [Decimal("12.00")]
    assert [c.net for c in fin.combined_totals] == [Decimal("12.00")]


def test_org_financials_membership_refund_reduces_net(organization: Organization, member_user: RevelUser) -> None:
    """A refund is reported separately and comes off net (gross stays pre-refund)."""
    _membership_payment(organization, member_user, "30.00", refund_amount="10.00")

    fin = organization_financials(_scope(organization), currency=None, sort="revenue", order="desc")

    memberships = fin.memberships[0]
    assert memberships.gross == Decimal("30.00")
    assert memberships.refunded_amount == Decimal("10.00")
    assert memberships.net == Decimal("20.00")
    assert fin.combined_totals[0].net == Decimal("20.00")


def test_org_financials_fully_refunded_membership_still_counts_toward_gross(
    organization: Organization, member_user: RevelUser
) -> None:
    """A fully refunded payment keeps its original amount in gross; net goes to zero."""
    payment = _membership_payment(organization, member_user, "30.00", refund_amount="30.00")
    payment.status = MembershipPayment.PaymentStatus.REFUNDED
    payment.save(update_fields=["status"])

    fin = organization_financials(_scope(organization), currency=None, sort="revenue", order="desc")

    memberships = fin.memberships[0]
    assert memberships.gross == Decimal("30.00")
    assert memberships.payment_count == 1
    assert memberships.refunded_amount == Decimal("30.00")
    assert memberships.net == Decimal("0.00")


def test_org_financials_currency_filter_scopes_memberships(
    organization: Organization,
    member_user: RevelUser,
    public_user: RevelUser,
) -> None:
    """``?currency=`` narrows the membership block and combined totals too."""
    _membership_payment(organization, member_user, "30.00", currency="EUR")
    _membership_payment(organization, public_user, "40.00", currency="USD", plan_name="Monthly USD")

    fin = organization_financials(_scope(organization), currency="USD", sort="revenue", order="desc")

    assert [m.currency for m in fin.memberships] == ["USD"]
    assert [c.currency for c in fin.combined_totals] == ["USD"]
    assert set(fin.available_currencies) == {"EUR", "USD"}


def test_org_financials_membership_outside_period_is_excluded(
    organization: Organization, member_user: RevelUser
) -> None:
    """Payments outside the window contribute nothing (period filter honoured)."""
    _membership_payment(organization, member_user, "30.00")
    past = ReportScope(org=organization, event_id=None, date_from=dt.date(2001, 1, 1), date_to=dt.date(2001, 12, 31))

    fin = organization_financials(past, currency=None, sort="revenue", order="desc")

    assert fin.memberships == []
    assert fin.combined_totals == []
