"""Fixtures shared by the notification eligibility test modules."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    Organization,
    OrganizationStaff,
    Ticket,
    TicketTier,
)


@pytest.fixture
def owner(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization owner."""
    return django_user_model.objects.create_user(
        username="owner@example.com",
        email="owner@example.com",
        password="password",
    )


@pytest.fixture
def staff_user(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization staff member."""
    return django_user_model.objects.create_user(
        username="staff@example.com",
        email="staff@example.com",
        password="password",
    )


@pytest.fixture
def member_user(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization member."""
    return django_user_model.objects.create_user(
        username="member@example.com",
        email="member@example.com",
        password="password",
    )


@pytest.fixture
def rsvp_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User with RSVP."""
    return django_user_model.objects.create_user(
        username="rsvp@example.com",
        email="rsvp@example.com",
        password="password",
    )


@pytest.fixture
def ticket_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User with ticket."""
    return django_user_model.objects.create_user(
        username="ticket@example.com",
        email="ticket@example.com",
        password="password",
    )


@pytest.fixture
def invited_user(django_user_model: type[RevelUser]) -> RevelUser:
    """Invited user."""
    return django_user_model.objects.create_user(
        username="invited@example.com",
        email="invited@example.com",
        password="password",
    )


@pytest.fixture
def random_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User with no participation."""
    return django_user_model.objects.create_user(
        username="random@example.com",
        email="random@example.com",
        password="password",
    )


@pytest.fixture
def organization(owner: RevelUser) -> Organization:
    """Test organization."""
    return Organization.objects.create(
        name="Test Organization",
        slug="test-org",
        owner=owner,
    )


@pytest.fixture
def event(organization: Organization) -> Event:
    """Test event."""
    next_week = timezone.now() + timedelta(days=7)
    return Event.objects.create(
        organization=organization,
        name="Test Event",
        slug="test-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=next_week,
        end=next_week + timedelta(hours=2),
        requires_ticket=True,
    )


@pytest.fixture
def ticket_tier(event: Event) -> TicketTier:
    """Ticket tier for the event (auto-created by signal when requires_ticket=True)."""
    # Signal auto-creates "General Admission" tier when event.requires_ticket=True
    return TicketTier.objects.get(event=event, name="General Admission")


@pytest.fixture
def offline_tier(event: Event) -> TicketTier:
    """Offline payment tier for testing pending tickets."""
    return TicketTier.objects.create(
        event=event,
        name="Offline Tier",
        visibility=TicketTier.Visibility.PUBLIC,
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        price=10,
    )


@pytest.fixture
def online_tier(event: Event) -> TicketTier:
    """Online payment tier for testing pending tickets."""
    return TicketTier.objects.create(
        event=event,
        name="Online Tier",
        visibility=TicketTier.Visibility.PUBLIC,
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price=10,
    )


@pytest.fixture
def setup_participants(
    organization: Organization,
    event: Event,
    staff_user: RevelUser,
    rsvp_user: RevelUser,
    ticket_user: RevelUser,
    invited_user: RevelUser,
    ticket_tier: TicketTier,
) -> dict[str, RevelUser]:
    """Set up all types of event participants.

    Note: Organization members are NOT participants by default - they must also
    have an RSVP, ticket, or invitation to be considered participating.
    """
    # Staff member
    OrganizationStaff.objects.create(
        organization=organization,
        user=staff_user,
    )

    # RSVP
    EventRSVP.objects.create(
        event=event,
        user=rsvp_user,
        status=EventRSVP.RsvpStatus.YES,
    )

    # Ticket
    Ticket.objects.create(
        event=event,
        user=ticket_user,
        tier=ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name="Ticket Holder",
    )

    # Invitation
    EventInvitation.objects.create(
        event=event,
        user=invited_user,
    )

    return {
        "staff": staff_user,
        "rsvp": rsvp_user,
        "ticket": ticket_user,
        "invited": invited_user,
    }
