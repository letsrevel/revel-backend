"""Tests for the org financials endpoint (#551 addendum)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier

pytestmark = pytest.mark.django_db


def _url(org: Organization) -> str:
    return reverse("api:organization_financials", kwargs={"slug": org.slug})


def _online(user: RevelUser, event: Event, tier: TicketTier, amount: str, currency: str = "EUR") -> None:
    ticket = Ticket.objects.create(guest_name="g", user=user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE)
    Payment.objects.create(
        ticket=ticket, user=user, stripe_session_id="s", amount=Decimal(amount),
        platform_fee=Decimal("0.50"), currency=currency, status=Payment.PaymentStatus.SUCCEEDED,
    )


def test_financials_default_current_year(
    organization_owner_client: Client, organization: Organization, event: Event,
    event_ticket_tier: TicketTier, public_user: RevelUser,
) -> None:
    _online(public_user, event, event_ticket_tier, "30.00")
    response = organization_owner_client.get(_url(organization))
    assert response.status_code == 200
    body = response.json()
    assert body["active_currency"] == "EUR"
    assert body["date_from"] == f"{dt.date.today().year}-01-01"
    assert body["available_currencies"] == ["EUR"]
    assert len(body["events"]) == 1
    eur = body["events"][0]["by_currency"][0]
    assert Decimal(eur["gross"]) == Decimal("30.00")


def test_financials_month_and_quarter_is_422(
    organization_owner_client: Client, organization: Organization,
) -> None:
    response = organization_owner_client.get(_url(organization), {"month": 1, "quarter": 1})
    assert response.status_code == 422


def test_financials_currency_filter(
    organization_owner_client: Client, organization: Organization, event: Event,
    event_ticket_tier: TicketTier, public_user: RevelUser, member_user: RevelUser,
) -> None:
    _online(public_user, event, event_ticket_tier, "100.00", currency="EUR")
    _online(member_user, event, event_ticket_tier, "5.00", currency="USD")
    response = organization_owner_client.get(_url(organization), {"currency": "USD"})
    assert response.status_code == 200
    body = response.json()
    assert body["active_currency"] == "USD"
    assert [t["currency"] for t in body["totals"]] == ["USD"]


def test_financials_requires_manage_organization(
    member_client: Client, organization: Organization,
) -> None:
    response = member_client.get(_url(organization))
    assert response.status_code == 403
