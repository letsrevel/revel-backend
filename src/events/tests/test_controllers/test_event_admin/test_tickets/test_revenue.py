"""Tests for the per-event financials endpoint (#551 addendum; replaces #515 shape)."""

import typing as t
from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Payment, Ticket, TicketTier

pytestmark = pytest.mark.django_db


def _make_online_ticket(
    *,
    user: RevelUser,
    event: Event,
    tier: TicketTier,
    amount: Decimal,
    currency: str = "EUR",
    status: Payment.PaymentStatus = Payment.PaymentStatus.SUCCEEDED,
    refund_amount: Decimal | None = None,
    refund_status: Payment.RefundStatus | None = None,
) -> Payment:
    ticket = Ticket.objects.create(
        guest_name="Online Guest", user=user, event=event, tier=tier, status=Ticket.TicketStatus.ACTIVE,
    )
    # The unified engine requires refunded_at to count a refund in the period window.
    refunded_at = timezone.now() if refund_status == Payment.RefundStatus.SUCCEEDED else None
    return Payment.objects.create(
        ticket=ticket, user=user, stripe_session_id="sess", amount=amount,
        platform_fee=Decimal("0.50"), currency=currency, status=status,
        refund_amount=refund_amount, refund_status=refund_status, refunded_at=refunded_at,
    )


def _revenue_url(event: Event) -> str:
    return reverse("api:event_revenue", kwargs={"event_id": event.pk})


def _by_currency(data: dict[str, t.Any]) -> dict[str, dict[str, t.Any]]:
    return {row["currency"]: row for row in data["by_currency"]}


def test_revenue_online_only(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier,
    public_user: RevelUser, member_user: RevelUser,
) -> None:
    _make_online_ticket(user=public_user, event=event, tier=event_ticket_tier, amount=Decimal("10.00"))
    _make_online_ticket(user=member_user, event=event, tier=event_ticket_tier, amount=Decimal("15.00"))
    response = organization_owner_client.get(_revenue_url(event))
    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] == str(event.pk)
    eur = _by_currency(body)["EUR"]
    assert Decimal(eur["gross"]) == Decimal("25.00")
    assert Decimal(eur["refunds"]) == Decimal("0.00")
    assert Decimal(eur["net"]) == Decimal("25.00")
    assert eur["sold_count"] == 2
    assert eur["refunded_count"] == 0


def test_revenue_partial_refund(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier,
    public_user: RevelUser, member_user: RevelUser,
) -> None:
    _make_online_ticket(user=public_user, event=event, tier=event_ticket_tier, amount=Decimal("20.00"))
    _make_online_ticket(
        user=member_user, event=event, tier=event_ticket_tier, amount=Decimal("10.00"),
        status=Payment.PaymentStatus.REFUNDED, refund_amount=Decimal("4.00"),
        refund_status=Payment.RefundStatus.SUCCEEDED,
    )
    eur = _by_currency(organization_owner_client.get(_revenue_url(event)).json())["EUR"]
    assert Decimal(eur["gross"]) == Decimal("30.00")
    assert Decimal(eur["refunds"]) == Decimal("4.00")
    assert Decimal(eur["net"]) == Decimal("26.00")
    assert eur["sold_count"] == 2
    assert eur["refunded_count"] == 1


def test_revenue_multi_currency_sorted(
    organization_owner_client: Client, event: Event, event_ticket_tier: TicketTier,
    public_user: RevelUser, member_user: RevelUser,
) -> None:
    _make_online_ticket(user=public_user, event=event, tier=event_ticket_tier, amount=Decimal("10.00"), currency="EUR")
    _make_online_ticket(user=member_user, event=event, tier=event_ticket_tier, amount=Decimal("20.00"), currency="USD")
    body = organization_owner_client.get(_revenue_url(event)).json()
    assert [row["currency"] for row in body["by_currency"]] == ["EUR", "USD"]


def test_revenue_empty_event(organization_owner_client: Client, event: Event) -> None:
    body = organization_owner_client.get(_revenue_url(event)).json()
    assert body["event_id"] == str(event.pk)
    assert body["by_currency"] == []


def test_revenue_offline_full_refund_nets_out(
    organization_owner_client: Client, event: Event, offline_tier: TicketTier, public_user: RevelUser,
) -> None:
    from events.service import ticket_service

    ticket = Ticket.objects.create(
        guest_name="g", user=public_user, event=event, tier=offline_tier, status=Ticket.TicketStatus.ACTIVE,
    )
    ticket_service.mark_offline_ticket_refunded(ticket, cancelled_by=public_user)
    eur = _by_currency(organization_owner_client.get(_revenue_url(event)).json())["EUR"]
    assert Decimal(eur["gross"]) == Decimal("25.00")
    assert Decimal(eur["refunds"]) == Decimal("25.00")
    assert Decimal(eur["net"]) == Decimal("0.00")
    assert eur["refunded_count"] == 1


def test_revenue_requires_manage_tickets(member_client: Client, event: Event) -> None:
    assert member_client.get(_revenue_url(event)).status_code == 403
