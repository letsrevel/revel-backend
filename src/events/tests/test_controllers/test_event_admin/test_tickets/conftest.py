"""Shared fixtures for ticket tests."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketTier


@pytest.fixture
def offline_tier(event: Event) -> TicketTier:
    """Create an offline payment ticket tier."""
    return TicketTier.objects.create(
        event=event,
        name="Offline Payment",
        price=25.00,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def at_door_tier(event: Event) -> TicketTier:
    """Create an at-the-door payment ticket tier."""
    return TicketTier.objects.create(
        event=event,
        name="At The Door",
        price=30.00,
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
    )


@pytest.fixture
def pending_offline_ticket(public_user: RevelUser, event: Event, offline_tier: TicketTier) -> Ticket:
    """Create a pending ticket for offline payment."""
    return Ticket.objects.create(
        guest_name="Test Guest",
        user=public_user,
        event=event,
        tier=offline_tier,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def pending_at_door_ticket(member_user: RevelUser, event: Event, at_door_tier: TicketTier) -> Ticket:
    """Create a pending ticket for at-the-door payment."""
    return Ticket.objects.create(
        guest_name="Test Guest",
        user=member_user,
        event=event,
        tier=at_door_tier,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def active_online_ticket(organization_staff_user: RevelUser, event: Event, event_ticket_tier: TicketTier) -> Ticket:
    """Create an active ticket for online payment (should not appear in pending list)."""
    return Ticket.objects.create(
        guest_name="Test Guest",
        user=organization_staff_user,
        event=event,
        tier=event_ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
    )


@pytest.fixture
def pwyc_offline_tier(event: Event) -> TicketTier:
    """Create a PWYC tier with offline payment."""
    return TicketTier.objects.create(
        event=event,
        name="PWYC Offline",
        price=Decimal("0"),
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5"),
        pwyc_max=Decimal("50"),
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def pending_pwyc_offline_ticket(public_user: RevelUser, event: Event, pwyc_offline_tier: TicketTier) -> Ticket:
    """Create a pending ticket linked to a PWYC offline tier."""
    return Ticket.objects.create(
        guest_name="PWYC Guest",
        user=public_user,
        event=event,
        tier=pwyc_offline_tier,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def pwyc_at_door_tier(event: Event) -> TicketTier:
    """Create a PWYC tier with at-the-door payment."""
    return TicketTier.objects.create(
        event=event,
        name="PWYC At Door",
        price=Decimal("0"),
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5"),
        pwyc_max=Decimal("50"),
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
    )


@pytest.fixture
def pending_pwyc_at_door_ticket(member_user: RevelUser, event: Event, pwyc_at_door_tier: TicketTier) -> Ticket:
    """Create a pending ticket linked to a PWYC at-the-door tier."""
    return Ticket.objects.create(
        guest_name="PWYC Door Guest",
        user=member_user,
        event=event,
        tier=pwyc_at_door_tier,
        status=Ticket.TicketStatus.PENDING,
    )


@pytest.fixture
def pending_pwyc_offline_ticket_with_price(
    public_user: RevelUser, event: Event, pwyc_offline_tier: TicketTier
) -> Ticket:
    """Create a pending PWYC offline ticket with price_paid already set.

    This simulates the realistic batch-checkout scenario where the user
    chose a price during checkout but payment hasn't been confirmed yet.
    """
    return Ticket.objects.create(
        guest_name="PWYC Priced Guest",
        user=public_user,
        event=event,
        tier=pwyc_offline_tier,
        status=Ticket.TicketStatus.PENDING,
        price_paid=Decimal("10.00"),
    )
