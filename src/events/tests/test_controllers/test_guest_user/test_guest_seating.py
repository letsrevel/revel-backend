"""Tests for reserved seating functionality for guest ticket checkout endpoints."""

from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events import schema
from events.models import Event, Organization, Ticket, TicketTier, Venue, VenueSeat, VenueSector
from events.service import guest as guest_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def venue(organization: Organization) -> Venue:
    """Create a venue for testing."""
    return Venue.objects.create(
        organization=organization,
        name="Test Venue",
        capacity=100,
    )


@pytest.fixture
def sector(venue: Venue) -> VenueSector:
    """Create a sector for testing."""
    return VenueSector.objects.create(
        venue=venue,
        name="Test Sector",
        capacity=50,
    )


@pytest.fixture
def seats(sector: VenueSector) -> list[VenueSeat]:
    """Create seats for testing."""
    seats_list = []
    for i in range(5):
        seats_list.append(
            VenueSeat.objects.create(
                sector=sector,
                label=f"A{i + 1}",
                row="A",
                number=str(i + 1),
            )
        )
    return seats_list


@pytest.fixture
def user_choice_tier(
    guest_event_with_tickets: Event,
    venue: Venue,
    sector: VenueSector,
) -> TicketTier:
    """A tier with USER_CHOICE seat assignment mode."""
    tier = TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="Reserved Seating",
        price=Decimal("25.00"),
        payment_method=TicketTier.PaymentMethod.FREE,  # Free for easier testing
        price_type=TicketTier.PriceType.FIXED,
        venue=venue,
        sector=sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        max_tickets_per_user=10,  # Allow multiple tickets per user for testing
    )
    return tier


@pytest.fixture
def random_seat_tier(
    guest_event_with_tickets: Event,
    venue: Venue,
    sector: VenueSector,
) -> TicketTier:
    """A tier with RANDOM seat assignment mode."""
    tier = TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="Random Seating",
        price=Decimal("20.00"),
        payment_method=TicketTier.PaymentMethod.FREE,  # Free for easier testing
        price_type=TicketTier.PriceType.FIXED,
        venue=venue,
        sector=sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.RANDOM,
    )
    return tier


class TestGuestCheckoutReservedSeating:
    """Test reserved seating functionality for guest ticket checkout endpoints."""

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_guest_checkout_user_choice_seating_success(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        user_choice_tier: TicketTier,
        seats: list[VenueSeat],
    ) -> None:
        """Test guest checkout with USER_CHOICE seating includes seat in confirmation."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": user_choice_tier.pk},
        )
        payload = {
            "email": "reservedseat@example.com",
            "first_name": "Reserved",
            "last_name": "Seat",
            "tickets": [{"guest_name": "Reserved Seat", "seat_id": str(seats[0].id)}],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "message" in data  # Email confirmation response

        # Check email was sent
        mock_send_email.assert_called_once()

    def test_guest_checkout_confirm_with_seat_assignment(
        self,
        guest_event_with_tickets: Event,
        user_choice_tier: TicketTier,
        seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """Test confirming guest ticket with seat creates ticket with assigned seat."""
        # Arrange: Create token with seat info
        tickets = [schema.TicketPurchaseItem(guest_name="Seat Test", seat_id=seats[0].id)]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, user_choice_tier.id, tickets
        )

        # Act: Confirm the token
        client = Client()
        url = reverse("api:confirm_guest_action")
        response = client.post(url, data={"token": token}, content_type="application/json")

        # Assert - BatchCheckoutResponse for consistency
        assert response.status_code == 200
        data = response.json()
        assert "tickets" in data
        assert len(data["tickets"]) == 1
        assert data["tickets"][0]["seat"]["id"] == str(seats[0].id)
        assert data["tickets"][0]["seat"]["label"] == "A1"

        # Verify ticket was created with seat
        ticket = Ticket.objects.get(user=existing_guest_user, event=guest_event_with_tickets)
        assert ticket.seat == seats[0]
        assert ticket.guest_name == "Seat Test"

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_guest_checkout_random_seating_success(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        random_seat_tier: TicketTier,
        seats: list[VenueSeat],
    ) -> None:
        """Test guest checkout with RANDOM seating mode (no seat_id needed)."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": random_seat_tier.pk},
        )
        payload = {
            "email": "randomseat@example.com",
            "first_name": "Random",
            "last_name": "Seat",
            "tickets": [{"guest_name": "Random Seat"}],  # No seat_id for RANDOM mode
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        mock_send_email.assert_called_once()

    def test_guest_checkout_confirm_random_seating(
        self,
        guest_event_with_tickets: Event,
        random_seat_tier: TicketTier,
        seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """Test confirming guest ticket with RANDOM seating assigns seat automatically."""
        # Arrange: Create token without seat_id (RANDOM mode)
        tickets = [schema.TicketPurchaseItem(guest_name="Random Test")]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, random_seat_tier.id, tickets
        )

        # Act: Confirm the token
        client = Client()
        url = reverse("api:confirm_guest_action")
        response = client.post(url, data={"token": token}, content_type="application/json")

        # Assert - BatchCheckoutResponse for consistency
        assert response.status_code == 200
        data = response.json()
        assert "tickets" in data
        assert len(data["tickets"]) == 1
        # Seat should be assigned randomly
        assert data["tickets"][0]["seat"] is not None
        assert "label" in data["tickets"][0]["seat"]

        # Verify ticket was created with a seat
        ticket = Ticket.objects.get(user=existing_guest_user, event=guest_event_with_tickets)
        assert ticket.seat is not None
        assert ticket.seat in seats

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_guest_checkout_multiple_tickets_with_seats(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        user_choice_tier: TicketTier,
        seats: list[VenueSeat],
    ) -> None:
        """Test guest checkout with multiple tickets each having their own seat."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": user_choice_tier.pk},
        )
        payload = {
            "email": "multiticket@example.com",
            "first_name": "Multi",
            "last_name": "Ticket",
            "tickets": [
                {"guest_name": "Guest 1", "seat_id": str(seats[0].id)},
                {"guest_name": "Guest 2", "seat_id": str(seats[1].id)},
                {"guest_name": "Guest 3", "seat_id": str(seats[2].id)},
            ],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        mock_send_email.assert_called_once()

    def test_guest_checkout_confirm_multiple_tickets_with_seats(
        self,
        guest_event_with_tickets: Event,
        user_choice_tier: TicketTier,
        seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """Test confirming multiple tickets creates all with correct seats."""
        # Arrange
        tickets = [
            schema.TicketPurchaseItem(guest_name="Guest 1", seat_id=seats[0].id),
            schema.TicketPurchaseItem(guest_name="Guest 2", seat_id=seats[1].id),
        ]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, user_choice_tier.id, tickets
        )

        # Act
        client = Client()
        url = reverse("api:confirm_guest_action")
        response = client.post(url, data={"token": token}, content_type="application/json")

        # Assert - BatchCheckoutResponse for multiple tickets
        assert response.status_code == 200
        data = response.json()
        assert "tickets" in data
        assert len(data["tickets"]) == 2

        # Verify all tickets created with correct seats
        created_tickets = Ticket.objects.filter(user=existing_guest_user, event=guest_event_with_tickets).order_by(
            "guest_name"
        )
        assert created_tickets.count() == 2
        assert created_tickets[0].guest_name == "Guest 1"
        assert created_tickets[0].seat == seats[0]
        assert created_tickets[1].guest_name == "Guest 2"
        assert created_tickets[1].seat == seats[1]

    def test_guest_checkout_user_choice_requires_seat_id_at_confirmation(
        self,
        guest_event_with_tickets: Event,
        user_choice_tier: TicketTier,
        seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """Test USER_CHOICE mode requires seat_id - validation happens at confirmation time for non-online."""
        # For non-online payments, checkout just creates a token (no validation)
        # Validation happens when the token is confirmed

        # Arrange: Create token without seat_id (invalid for USER_CHOICE mode)
        tickets = [schema.TicketPurchaseItem(guest_name="No Seat")]  # Missing seat_id
        token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, user_choice_tier.id, tickets
        )

        # Act: Try to confirm the token
        client = Client()
        url = reverse("api:confirm_guest_action")
        response = client.post(url, data={"token": token}, content_type="application/json")

        # Assert - Should fail because USER_CHOICE requires seat_id
        assert response.status_code == 400
        data = response.json()
        assert "seat" in data["detail"].lower()

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_guest_checkout_online_with_reserved_seating(
        self,
        mock_stripe: Mock,
        guest_event_with_tickets: Event,
        venue: Venue,
        sector: VenueSector,
        seats: list[VenueSeat],
    ) -> None:
        """Test guest online checkout with reserved seating creates pending tickets with seats."""
        # Arrange - Create online tier with USER_CHOICE seating
        online_user_choice_tier = TicketTier.objects.create(
            event=guest_event_with_tickets,
            name="Online Reserved",
            price=Decimal("50.00"),
            payment_method=TicketTier.PaymentMethod.ONLINE,
            price_type=TicketTier.PriceType.FIXED,
            venue=venue,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            max_tickets_per_user=10,  # Allow multiple tickets per user
        )

        checkout_url = "https://checkout.stripe.com/reserved"
        mock_stripe.return_value = checkout_url

        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": online_user_choice_tier.pk},
        )
        payload = {
            "email": "onlinereserved@example.com",
            "first_name": "Online",
            "last_name": "Reserved",
            "tickets": [
                {"guest_name": "Online Guest 1", "seat_id": str(seats[0].id)},
                {"guest_name": "Online Guest 2", "seat_id": str(seats[1].id)},
            ],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["checkout_url"] == checkout_url

        # Verify tickets were created with seats (pending until Stripe confirms)
        user = RevelUser.objects.get(email="onlinereserved@example.com")
        created_tickets = Ticket.objects.filter(user=user, event=guest_event_with_tickets)
        assert created_tickets.count() == 2
        # Check seats are assigned
        assigned_seats = {t.seat for t in created_tickets}
        assert seats[0] in assigned_seats
        assert seats[1] in assigned_seats
