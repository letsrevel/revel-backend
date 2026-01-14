"""Fixtures for guest user endpoint tests.

Tests cover:
- Guest RSVP (with email confirmation)
- Guest ticket checkout (fixed-price and PWYC)
- Guest action confirmation via JWT tokens
- Service layer functions for guest user handling
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from accounts.models import RevelUser
from events.models import Event, Organization, TicketTier


@pytest.fixture
def guest_event(organization: Organization, next_week: datetime) -> Event:
    """An event that allows guest access (can_attend_without_login=True)."""
    return Event.objects.create(
        organization=organization,
        name="Guest-Friendly Event",
        slug="guest-friendly-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=next_week,
        end=next_week + timedelta(days=1),
        max_attendees=100,
        can_attend_without_login=True,  # Key: allows guests
        requires_ticket=False,  # Allows RSVP
    )


@pytest.fixture
def guest_event_with_tickets(organization: Organization, next_week: datetime) -> Event:
    """An event that allows guest access and requires tickets."""
    return Event.objects.create(
        organization=organization,
        name="Guest Ticketed Event",
        slug="guest-ticketed-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=next_week,
        end=next_week + timedelta(days=1),
        max_attendees=100,
        can_attend_without_login=True,
        requires_ticket=True,
    )


@pytest.fixture
def login_required_event(organization: Organization, next_week: datetime) -> Event:
    """An event that does NOT allow guest access (can_attend_without_login=False)."""
    return Event.objects.create(
        organization=organization,
        name="Login Required Event",
        slug="login-required-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=next_week,
        end=next_week + timedelta(days=1),
        max_attendees=100,
        can_attend_without_login=False,  # Key: requires login
    )


@pytest.fixture
def free_tier(guest_event_with_tickets: Event) -> TicketTier:
    """A free ticket tier (no payment required)."""
    return TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="Free Tier",
        price=Decimal("0.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        price_type=TicketTier.PriceType.FIXED,
    )


@pytest.fixture
def offline_tier(guest_event_with_tickets: Event) -> TicketTier:
    """An offline payment tier."""
    return TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="Offline Tier",
        price=Decimal("10.00"),
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        price_type=TicketTier.PriceType.FIXED,
    )


@pytest.fixture
def online_tier(guest_event_with_tickets: Event) -> TicketTier:
    """An online payment tier (Stripe)."""
    return TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="Online Tier",
        price=Decimal("20.00"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price_type=TicketTier.PriceType.FIXED,
    )


@pytest.fixture
def pwyc_tier(guest_event_with_tickets: Event) -> TicketTier:
    """A pay-what-you-can tier with offline payment."""
    return TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="PWYC Tier",
        price=Decimal("0.00"),
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("5.00"),
        pwyc_max=Decimal("50.00"),
    )


@pytest.fixture
def pwyc_online_tier(guest_event_with_tickets: Event) -> TicketTier:
    """A pay-what-you-can tier with online payment."""
    return TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="PWYC Online Tier",
        price=Decimal("0.00"),
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price_type=TicketTier.PriceType.PWYC,
        pwyc_min=Decimal("10.00"),
        pwyc_max=Decimal("100.00"),
    )


@pytest.fixture
def existing_regular_user(django_user_model: type[RevelUser]) -> RevelUser:
    """An existing non-guest user (to test email conflicts)."""
    return django_user_model.objects.create_user(
        username="existing@example.com",
        email="existing@example.com",
        password="password123",
        first_name="Existing",
        last_name="User",
        guest=False,
    )


@pytest.fixture
def existing_guest_user(django_user_model: type[RevelUser]) -> RevelUser:
    """An existing guest user."""
    return django_user_model.objects.create_user(
        username="guest@example.com",
        email="guest@example.com",
        password="",
        first_name="Old",
        last_name="Name",
        guest=True,
    )
