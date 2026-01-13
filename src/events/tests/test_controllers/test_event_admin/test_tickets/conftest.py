"""Shared fixtures for ticket tests."""

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
