"""Tests for the per-event revenue aggregate endpoint (#515)."""

from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse

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
    """Create an ACTIVE online ticket with an attached Payment."""
    ticket = Ticket.objects.create(
        guest_name="Online Guest",
        user=user,
        event=event,
        tier=tier,
        status=Ticket.TicketStatus.ACTIVE,
    )
    return Payment.objects.create(
        ticket=ticket,
        user=user,
        stripe_session_id="sess",
        amount=amount,
        platform_fee=Decimal("0.50"),
        currency=currency,
        status=status,
        refund_amount=refund_amount,
        refund_status=refund_status,
    )


def _revenue_url(event: Event) -> str:
    return reverse("api:event_revenue", kwargs={"event_id": event.pk})


def _by_currency(data: dict) -> dict[str, dict]:
    return {row["currency"]: row for row in data["by_currency"]}


def test_revenue_online_only(
    organization_owner_client: Client,
    event: Event,
    event_ticket_tier: TicketTier,
    public_user: RevelUser,
    member_user: RevelUser,
) -> None:
    """Two successful online payments sum into one gross figure."""
    _make_online_ticket(user=public_user, event=event, tier=event_ticket_tier, amount=Decimal("10.00"))
    _make_online_ticket(user=member_user, event=event, tier=event_ticket_tier, amount=Decimal("15.00"))

    response = organization_owner_client.get(_revenue_url(event))
    assert response.status_code == 200
    rows = _by_currency(response.json())
    assert set(rows) == {"EUR"}
    eur = rows["EUR"]
    assert Decimal(eur["gross"]) == Decimal("25.00")
    assert Decimal(eur["refunded"]) == Decimal("0.00")
    assert Decimal(eur["net"]) == Decimal("25.00")
    assert eur["paid_ticket_count"] == 2


def test_revenue_offline_only(
    organization_owner_client: Client,
    event: Event,
    offline_tier: TicketTier,
    at_door_tier: TicketTier,
    public_user: RevelUser,
    member_user: RevelUser,
    nonmember_user: RevelUser,
) -> None:
    """Offline ACTIVE and at-the-door CHECKED_IN count; ACTIVE at-the-door and pending do not."""
    # Offline confirmed -> paid (tier price 25)
    Ticket.objects.create(
        guest_name="g", user=public_user, event=event, tier=offline_tier, status=Ticket.TicketStatus.ACTIVE
    )
    # At-the-door checked in -> paid (tier price 30)
    Ticket.objects.create(
        guest_name="g", user=member_user, event=event, tier=at_door_tier, status=Ticket.TicketStatus.CHECKED_IN
    )
    # At-the-door ACTIVE but not checked in -> NOT yet paid
    Ticket.objects.create(
        guest_name="g", user=nonmember_user, event=event, tier=at_door_tier, status=Ticket.TicketStatus.ACTIVE
    )
    # Pending offline -> not paid
    Ticket.objects.create(
        guest_name="g", user=nonmember_user, event=event, tier=offline_tier, status=Ticket.TicketStatus.PENDING
    )

    response = organization_owner_client.get(_revenue_url(event))
    assert response.status_code == 200
    rows = _by_currency(response.json())
    eur = rows["EUR"]
    assert Decimal(eur["gross"]) == Decimal("55.00")
    assert Decimal(eur["net"]) == Decimal("55.00")
    assert eur["paid_ticket_count"] == 2


def test_revenue_pwyc_price_paid_override(
    organization_owner_client: Client,
    event: Event,
    pwyc_offline_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """For PWYC offline tickets, price_paid is used over the (zero) tier price."""
    Ticket.objects.create(
        guest_name="g",
        user=public_user,
        event=event,
        tier=pwyc_offline_tier,
        status=Ticket.TicketStatus.ACTIVE,
        price_paid=Decimal("12.00"),
    )

    response = organization_owner_client.get(_revenue_url(event))
    assert response.status_code == 200
    eur = _by_currency(response.json())["EUR"]
    assert Decimal(eur["gross"]) == Decimal("12.00")
    assert eur["paid_ticket_count"] == 1


def test_revenue_partial_refund(
    organization_owner_client: Client,
    event: Event,
    event_ticket_tier: TicketTier,
    public_user: RevelUser,
    member_user: RevelUser,
) -> None:
    """Refunded payments stay in gross; the refunded amount is reported and netted out."""
    _make_online_ticket(user=public_user, event=event, tier=event_ticket_tier, amount=Decimal("20.00"))
    _make_online_ticket(
        user=member_user,
        event=event,
        tier=event_ticket_tier,
        amount=Decimal("10.00"),
        status=Payment.PaymentStatus.REFUNDED,
        refund_amount=Decimal("4.00"),
        refund_status=Payment.RefundStatus.SUCCEEDED,
    )

    response = organization_owner_client.get(_revenue_url(event))
    assert response.status_code == 200
    eur = _by_currency(response.json())["EUR"]
    assert Decimal(eur["gross"]) == Decimal("30.00")
    assert Decimal(eur["refunded"]) == Decimal("4.00")
    assert Decimal(eur["net"]) == Decimal("26.00")
    # Only the SUCCEEDED payment is a currently-held paid ticket.
    assert eur["paid_ticket_count"] == 1


def test_revenue_full_refund_still_listed(
    organization_owner_client: Client,
    event: Event,
    event_ticket_tier: TicketTier,
    public_user: RevelUser,
) -> None:
    """A fully-refunded online payment nets to zero but remains visible."""
    _make_online_ticket(
        user=public_user,
        event=event,
        tier=event_ticket_tier,
        amount=Decimal("10.00"),
        status=Payment.PaymentStatus.REFUNDED,
        refund_amount=Decimal("10.00"),
        refund_status=Payment.RefundStatus.SUCCEEDED,
    )

    response = organization_owner_client.get(_revenue_url(event))
    assert response.status_code == 200
    eur = _by_currency(response.json())["EUR"]
    assert Decimal(eur["gross"]) == Decimal("10.00")
    assert Decimal(eur["refunded"]) == Decimal("10.00")
    assert Decimal(eur["net"]) == Decimal("0.00")
    assert eur["paid_ticket_count"] == 0


def test_revenue_multi_currency_sorted(
    organization_owner_client: Client,
    event: Event,
    event_ticket_tier: TicketTier,
    public_user: RevelUser,
    member_user: RevelUser,
) -> None:
    """Different payment currencies produce separate, currency-sorted rows."""
    _make_online_ticket(user=public_user, event=event, tier=event_ticket_tier, amount=Decimal("10.00"), currency="EUR")
    _make_online_ticket(user=member_user, event=event, tier=event_ticket_tier, amount=Decimal("20.00"), currency="USD")

    response = organization_owner_client.get(_revenue_url(event))
    assert response.status_code == 200
    data = response.json()
    assert [row["currency"] for row in data["by_currency"]] == ["EUR", "USD"]
    rows = _by_currency(data)
    assert Decimal(rows["EUR"]["gross"]) == Decimal("10.00")
    assert Decimal(rows["USD"]["gross"]) == Decimal("20.00")


def test_revenue_empty_event(
    organization_owner_client: Client,
    event: Event,
) -> None:
    """An event with no tickets returns an empty list."""
    response = organization_owner_client.get(_revenue_url(event))
    assert response.status_code == 200
    assert response.json() == {"by_currency": []}


def test_revenue_pending_only_omitted(
    organization_owner_client: Client,
    event: Event,
    event_ticket_tier: TicketTier,
    offline_tier: TicketTier,
    public_user: RevelUser,
    member_user: RevelUser,
) -> None:
    """A currency whose only activity is pending/unpaid is omitted entirely."""
    _make_online_ticket(
        user=public_user,
        event=event,
        tier=event_ticket_tier,
        amount=Decimal("10.00"),
        status=Payment.PaymentStatus.PENDING,
    )
    Ticket.objects.create(
        guest_name="g", user=member_user, event=event, tier=offline_tier, status=Ticket.TicketStatus.PENDING
    )

    response = organization_owner_client.get(_revenue_url(event))
    assert response.status_code == 200
    assert response.json() == {"by_currency": []}


def test_revenue_requires_manage_tickets(
    member_client: Client,
    event: Event,
) -> None:
    """A plain member without manage_tickets cannot read revenue."""
    response = member_client.get(_revenue_url(event))
    assert response.status_code == 403
