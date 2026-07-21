"""Tests for reserved seating functionality for guest ticket checkout endpoints."""

from decimal import Decimal
from unittest import mock
from unittest.mock import Mock, patch
from uuid import UUID

import jwt
import pytest
from django.conf import settings
from django.test.client import Client
from django.urls import reverse

from accounts.jwt import create_token
from accounts.models import RevelUser
from events import schema
from events.models import (
    Event,
    Organization,
    PriceCategory,
    SeatHold,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)
from events.service import guest as guest_service
from events.service.guest_hold_session import GUEST_HOLD_COOKIE, issue_guest_hold_token
from events.service.seating import holds as holds_service

pytestmark = pytest.mark.django_db


def _fake_session(session_id: str = "cs_test123") -> mock.Mock:
    """A minimal stand-in for a ``stripe.checkout.Session``."""
    return mock.Mock(id=session_id, url=f"https://checkout.stripe.com/c/{session_id}")


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
                row_label="A",
                number=str(i + 1),
                adjacency_index=i,
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
def best_available_tier(
    guest_event_with_tickets: Event,
    venue: Venue,
    sector: VenueSector,
    seats: list[VenueSeat],
) -> TicketTier:
    """A tier with BEST_AVAILABLE seat assignment mode (system auto-assigns seats)."""
    guest_event_with_tickets.venue = venue
    guest_event_with_tickets.save(update_fields=["venue"])
    category = PriceCategory.objects.create(venue=venue, name="Standard", color="#00aa00")
    VenueSeat.objects.filter(id__in=[s.id for s in seats]).update(default_price_category=category)
    return TicketTier.objects.create(
        event=guest_event_with_tickets,
        name="Auto Seating",
        price=Decimal("20.00"),
        payment_method=TicketTier.PaymentMethod.FREE,  # Free for easier testing
        price_type=TicketTier.PriceType.FIXED,
        venue=venue,
        sector=sector,
        category_prices={str(category.id): "20.00"},
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
    )


def _zone(tier: TicketTier) -> UUID:
    """The single zone of ``best_available_tier``: v3 makes the buyer name it per request."""
    return UUID(next(iter(tier.category_prices)))


@pytest.mark.django_db(transaction=True)
class TestGuestCheckoutReservedSeating:
    """Test reserved seating functionality for guest ticket checkout endpoints.

    Uses ``transaction=True`` because the non-online checkout flow schedules
    ``send_guest_ticket_confirmation`` via ``transaction.on_commit``. In default
    pytest-django mode the wrapping transaction is rolled back and the callback
    never fires, breaking the ``mock_send_email`` assertions.
    """

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
    def test_guest_checkout_best_available_seating_success(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        best_available_tier: TicketTier,
        seats: list[VenueSeat],
    ) -> None:
        """Test guest checkout with BEST_AVAILABLE seating mode (no seat_id needed)."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": best_available_tier.pk},
        )
        payload = {
            "email": "autoseat@example.com",
            "first_name": "Auto",
            "last_name": "Seat",
            "price_category_id": str(_zone(best_available_tier)),
            "tickets": [{"guest_name": "Auto Seat"}],  # No seat_id for BEST_AVAILABLE mode
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        mock_send_email.assert_called_once()

    def test_guest_checkout_confirm_best_available_seating(
        self,
        guest_event_with_tickets: Event,
        best_available_tier: TicketTier,
        seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """Test confirming guest ticket with BEST_AVAILABLE seating assigns seat automatically."""
        # Arrange: Create token without seat_id (BEST_AVAILABLE mode)
        tickets = [schema.TicketPurchaseItem(guest_name="Auto Test")]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user,
            guest_event_with_tickets.id,
            best_available_tier.id,
            tickets,
            price_category_id=_zone(best_available_tier),
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
        # Seat should be auto-assigned
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

    def test_guest_checkout_online_with_reserved_seating(
        self,
        guest_event_with_tickets: Event,
        venue: Venue,
        sector: VenueSector,
        seats: list[VenueSeat],
    ) -> None:
        """Test guest online checkout with reserved seating reserves pending tickets with seats,
        then the checkout-session step returns the Stripe URL (#632).
        """
        # Arrange - Stripe-connect the organization, then create online tier with USER_CHOICE seating
        org = guest_event_with_tickets.organization
        org.stripe_account_id = "acct_test123"
        org.stripe_charges_enabled = True
        org.stripe_details_submitted = True
        org.save()
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

        # Act: reserve
        with mock.patch("stripe.checkout.Session.create") as mock_create:
            response = client.post(url, data=payload, content_type="application/json")
            mock_create.assert_not_called()

        # Assert: reserved, no Stripe call yet
        assert response.status_code == 200
        data = response.json()
        assert data["checkout_url"] is None
        assert data["requires_payment"] is True
        reservation_id = data["reservation_id"]
        assert UUID(reservation_id)

        # Verify PENDING tickets were created with seats already (held until Stripe confirms)
        user = RevelUser.objects.get(email="onlinereserved@example.com")
        created_tickets = Ticket.objects.filter(user=user, event=guest_event_with_tickets)
        assert created_tickets.count() == 2
        assert all(t.status == Ticket.TicketStatus.PENDING for t in created_tickets)
        # Check seats are assigned
        assigned_seats = {t.seat for t in created_tickets}
        assert seats[0] in assigned_seats
        assert seats[1] in assigned_seats

        # Act: create the Stripe session
        fake = _fake_session()
        session_url = reverse("api:guest_checkout_session", kwargs={"reservation_id": reservation_id})
        with mock.patch("stripe.checkout.Session.create", return_value=fake) as mock_create:
            session_response = client.post(session_url, content_type="application/json")
            mock_create.assert_called_once()

        # Assert: got the Stripe URL
        assert session_response.status_code == 200
        assert session_response.json()["checkout_url"] == fake.url


ACCESSIBLE_EXHAUSTED_MSG = "Not enough accessible seats available — please contact the organizer."


@pytest.fixture
def accessible_seats(seats: list[VenueSeat]) -> list[VenueSeat]:
    """Mark the last two seats accessible; returns them."""
    marked = seats[3:]
    VenueSeat.objects.filter(id__in=[s.id for s in marked]).update(is_accessible=True)
    for s in marked:
        s.refresh_from_db()
    return marked


@pytest.mark.django_db(transaction=True)
class TestGuestAccessibleSeating:
    """Accessible seating for guest email-confirm flows (#726).

    Uses ``transaction=True`` because guest checkout schedules the confirmation
    email via ``transaction.on_commit`` (see TestGuestCheckoutReservedSeating).
    """

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_guest_checkout_embeds_accessible_required_in_token(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        best_available_tier: TicketTier,
        accessible_seats: list[VenueSeat],
    ) -> None:
        """Checkout payload flag is embedded in the signed email token."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": best_available_tier.pk},
        )
        payload = {
            "email": "accessible@example.com",
            "first_name": "Access",
            "last_name": "Ible",
            "accessible_required": True,
            "price_category_id": str(_zone(best_available_tier)),
            "tickets": [{"guest_name": "Access Ible"}],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert: the token sent by email carries the flag
        assert response.status_code == 200
        mock_send_email.assert_called_once()
        token = mock_send_email.call_args[0][1]
        decoded = guest_service.validate_and_decode_guest_token(token)
        assert isinstance(decoded, schema.GuestTicketJWTPayloadSchema)
        assert decoded.accessible_required is True

    def test_confirm_assigns_accessible_seats_when_flag_set(
        self,
        guest_event_with_tickets: Event,
        best_available_tier: TicketTier,
        accessible_seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """Confirm-time best-available assignment uses the accessible pool when the flag is set."""
        # Arrange
        best_available_tier.max_tickets_per_user = 10
        best_available_tier.save(update_fields=["max_tickets_per_user"])
        tickets = [
            schema.TicketPurchaseItem(guest_name="Guest 1"),
            schema.TicketPurchaseItem(guest_name="Guest 2"),
        ]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user,
            guest_event_with_tickets.id,
            best_available_tier.id,
            tickets,
            accessible_required=True,
            price_category_id=_zone(best_available_tier),
        )

        # Act
        client = Client()
        response = client.post(
            reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json"
        )

        # Assert: both tickets landed on accessible seats (general pool untouched)
        assert response.status_code == 200
        created = Ticket.objects.filter(user=existing_guest_user, event=guest_event_with_tickets)
        assert created.count() == 2
        assert {t.seat_id for t in created} == {s.id for s in accessible_seats}

    def test_confirm_accessible_exhaustion_returns_409_with_distinct_message(
        self,
        guest_event_with_tickets: Event,
        best_available_tier: TicketTier,
        accessible_seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """When the accessible pool can't fit the block, 409 with the distinct message (no general fallback)."""
        # Arrange: 3 tickets but only 2 accessible seats (3 general seats remain free)
        best_available_tier.max_tickets_per_user = 10
        best_available_tier.save(update_fields=["max_tickets_per_user"])
        tickets = [schema.TicketPurchaseItem(guest_name=f"Guest {i}") for i in range(3)]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user,
            guest_event_with_tickets.id,
            best_available_tier.id,
            tickets,
            accessible_required=True,
            price_category_id=_zone(best_available_tier),
        )

        # Act
        client = Client()
        response = client.post(
            reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json"
        )

        # Assert: distinct 409, and no tickets were created from the general pool
        assert response.status_code == 409
        assert response.json()["detail"] == ACCESSIBLE_EXHAUSTED_MSG
        assert not Ticket.objects.filter(user=existing_guest_user, event=guest_event_with_tickets).exists()

    def test_confirm_without_flag_keeps_general_pool_behavior(
        self,
        guest_event_with_tickets: Event,
        best_available_tier: TicketTier,
        accessible_seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """Regression: tokens without the flag keep assigning from the general (non-accessible) pool."""
        # Arrange
        tickets = [schema.TicketPurchaseItem(guest_name="General Guest")]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user,
            guest_event_with_tickets.id,
            best_available_tier.id,
            tickets,
            price_category_id=_zone(best_available_tier),
        )

        # Act
        client = Client()
        response = client.post(
            reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json"
        )

        # Assert
        assert response.status_code == 200
        ticket = Ticket.objects.get(user=existing_guest_user, event=guest_event_with_tickets)
        assert ticket.seat is not None
        assert ticket.seat.is_accessible is False

    def test_guest_online_checkout_assigns_accessible_seats_directly(
        self,
        guest_event_with_tickets: Event,
        venue: Venue,
        best_available_tier: TicketTier,
        accessible_seats: list[VenueSeat],
    ) -> None:
        """Online tiers assign at direct checkout (no email confirm) — the flag applies there too."""
        # Arrange: Stripe-connected org + online BEST_AVAILABLE tier on the same price category
        org = guest_event_with_tickets.organization
        org.stripe_account_id = "acct_test123"
        org.stripe_charges_enabled = True
        org.stripe_details_submitted = True
        org.save()
        online_tier = TicketTier.objects.create(
            event=guest_event_with_tickets,
            name="Online Auto Seating",
            price=Decimal("20.00"),
            payment_method=TicketTier.PaymentMethod.ONLINE,
            price_type=TicketTier.PriceType.FIXED,
            venue=venue,
            sector=best_available_tier.sector,
            category_prices=dict(best_available_tier.category_prices),
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        )
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": online_tier.pk},
        )
        payload = {
            "email": "onlineaccessible@example.com",
            "first_name": "Online",
            "last_name": "Accessible",
            "accessible_required": True,
            "price_category_id": str(_zone(online_tier)),
            "tickets": [{"guest_name": "Online Accessible"}],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert: PENDING ticket reserved on an accessible seat
        assert response.status_code == 200
        assert response.json()["requires_payment"] is True
        user = RevelUser.objects.get(email="onlineaccessible@example.com")
        ticket = Ticket.objects.get(user=user, event=guest_event_with_tickets)
        assert ticket.status == Ticket.TicketStatus.PENDING
        assert ticket.seat_id in {s.id for s in accessible_seats}


class TestGuestConfirmSessionBinding:
    """Fix B: the confirmation token binds the hold-owner guest_session so the buyer's
    own holds are consumed even when the email is opened on a different device."""

    def test_confirm_prefers_token_session_over_request_cookie(
        self,
        guest_event_with_tickets: Event,
        user_choice_tier: TicketTier,
        seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """A live hold owned by the checkout session is consumed even when confirming
        with a different/absent guest cookie — because the token carries the session."""
        original_session = "gs_original_device"
        held = seats[0]
        # Seat holds key off the event's venue; a seated event has it set.
        guest_event_with_tickets.venue = held.sector.venue
        guest_event_with_tickets.save(update_fields=["venue"])
        result = holds_service.acquire_seats(
            guest_event_with_tickets, [held.id], user=None, guest_session=original_session
        )
        assert result.conflicts == []

        tickets = [schema.TicketPurchaseItem(guest_name="Cross Device", seat_id=held.id)]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user,
            guest_event_with_tickets.id,
            user_choice_tier.id,
            tickets,
            guest_session=original_session,
        )

        # Confirm from a "different device": no guest-hold cookie on this request.
        response = Client().post(
            reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json"
        )

        assert response.status_code == 200, response.content
        ticket = Ticket.objects.get(user=existing_guest_user, event=guest_event_with_tickets)
        assert ticket.seat == held
        # The buyer's own hold was consumed, not left dangling.
        assert not SeatHold.objects.filter(event=guest_event_with_tickets, seat=held).exists()

    def test_legacy_token_without_guest_session_still_confirms(
        self,
        guest_event_with_tickets: Event,
        user_choice_tier: TicketTier,
        seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """A pre-fix token whose JSON lacks guest_session decodes (default None) and confirms."""
        tickets = [schema.TicketPurchaseItem(guest_name="Legacy", seat_id=seats[1].id)]
        # Mint a real (post-fix) token, then strip the new key to emulate a legacy one.
        valid_token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, user_choice_tier.id, tickets
        )
        raw = jwt.decode(
            valid_token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], options={"verify_aud": False}
        )
        raw.pop("guest_session")  # simulate a token minted before this field existed
        token = create_token(raw, settings.SECRET_KEY, settings.JWT_ALGORITHM)

        response = Client().post(
            reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json"
        )

        assert response.status_code == 200, response.content
        ticket = Ticket.objects.get(user=existing_guest_user, event=guest_event_with_tickets)
        assert ticket.seat == seats[1]


@pytest.mark.django_db(transaction=True)
class TestGuestZoneSelection:
    """The best-available zone (#749) survives the checkout → email → confirm round trip."""

    @pytest.fixture
    def two_zone_tier(
        self,
        best_available_tier: TicketTier,
        seats: list[VenueSeat],
    ) -> tuple[TicketTier, PriceCategory, PriceCategory]:
        """Split ``best_available_tier``'s seats into a Front and a Back zone."""
        venue = best_available_tier.venue
        assert venue is not None
        front = PriceCategory.objects.get(id=UUID(next(iter(best_available_tier.category_prices))))
        back = PriceCategory.objects.create(venue=venue, name="Back", color="#aa0000")
        VenueSeat.objects.filter(id__in=[s.id for s in seats[3:]]).update(default_price_category=back)
        best_available_tier.category_prices = {str(front.id): "20.00", str(back.id): "10.00"}
        best_available_tier.max_tickets_per_user = 10
        best_available_tier.save(update_fields=["category_prices", "max_tickets_per_user"])
        return best_available_tier, front, back

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_checkout_embeds_the_zone_in_the_token(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        two_zone_tier: tuple[TicketTier, PriceCategory, PriceCategory],
        seats: list[VenueSeat],
    ) -> None:
        tier, _front, back = two_zone_tier
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": tier.pk},
        )
        payload = {
            "email": "zone@example.com",
            "first_name": "Zone",
            "last_name": "Picker",
            "price_category_id": str(back.id),
            "tickets": [{"guest_name": "Zone Picker"}],
        }

        response = Client().post(url, data=payload, content_type="application/json")

        assert response.status_code == 200, response.content
        token = mock_send_email.call_args[0][1]
        decoded = guest_service.validate_and_decode_guest_token(token)
        assert isinstance(decoded, schema.GuestTicketJWTPayloadSchema)
        assert decoded.price_category_id == back.id

    def test_confirm_assigns_a_seat_from_the_token_zone(
        self,
        guest_event_with_tickets: Event,
        two_zone_tier: tuple[TicketTier, PriceCategory, PriceCategory],
        seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        tier, _front, back = two_zone_tier
        token = guest_service.create_guest_ticket_token(
            existing_guest_user,
            guest_event_with_tickets.id,
            tier.id,
            [schema.TicketPurchaseItem(guest_name="Zone Picker")],
            price_category_id=back.id,
        )

        response = Client().post(
            reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json"
        )

        assert response.status_code == 200, response.content
        ticket = Ticket.objects.get(user=existing_guest_user, event=guest_event_with_tickets)
        assert ticket.seat is not None
        assert ticket.seat.default_price_category_id == back.id

    def test_checkout_rejects_a_zone_the_tier_does_not_price(
        self,
        guest_event_with_tickets: Event,
        two_zone_tier: tuple[TicketTier, PriceCategory, PriceCategory],
        venue: Venue,
        seats: list[VenueSeat],
    ) -> None:
        """A venue category that is not a zone of this tier is unsellable through it."""
        tier, _front, _back = two_zone_tier
        stranger = PriceCategory.objects.create(venue=venue, name="Boxes", color="#0000aa")
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": tier.pk},
        )
        payload = {
            "email": "zone@example.com",
            "first_name": "Zone",
            "last_name": "Picker",
            "price_category_id": str(stranger.id),
            "tickets": [{"guest_name": "Zone Picker"}],
        }

        response = Client().post(url, data=payload, content_type="application/json")

        assert response.status_code == 400, response.content
        assert "Back" in response.json()["detail"]

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_stale_hold_in_another_zone_does_not_dead_link_the_confirmation_email(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        two_zone_tier: tuple[TicketTier, PriceCategory, PriceCategory],
        seats: list[VenueSeat],
    ) -> None:
        """A non-online guest checkout must never promise an email whose link then 409s.

        Seat assignment is deferred to the confirmation click, so a cheerful
        "check your email" for a request the confirm would refuse strands the buyer
        on a different device, with no hold-release UI on the confirmation page.
        """
        tier, _front, back = two_zone_tier
        guest_event_with_tickets.max_tickets_per_user = 10  # the hold cap is the event's
        guest_event_with_tickets.save(update_fields=["max_tickets_per_user"])
        session_id, cookie = issue_guest_hold_token()
        # Browsed Front, then switched to Back — the hold endpoint only ever ADDS.
        holds_service.acquire_seats(
            guest_event_with_tickets, [seats[0].id, seats[1].id], user=None, guest_session=session_id
        )
        holds_service.acquire_seats(
            guest_event_with_tickets, [seats[3].id, seats[4].id], user=None, guest_session=session_id
        )
        client = Client()
        client.cookies[GUEST_HOLD_COOKIE] = cookie

        response = client.post(
            reverse(
                "api:guest_ticket_checkout",
                kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": tier.pk},
            ),
            data={
                "email": "stalezone@example.com",
                "first_name": "Stale",
                "last_name": "Zone",
                "price_category_id": str(back.id),
                "tickets": [{"guest_name": "Guest 1"}, {"guest_name": "Guest 2"}],
            },
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert response.json()["message"]  # "check your email"

        # The emailed link must actually work — same buyer, different device (no cookie).
        token = mock_send_email.call_args[0][1]
        confirm = Client().post(
            reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json"
        )

        assert confirm.status_code == 200, confirm.content
        created = Ticket.objects.filter(event=guest_event_with_tickets, user__email="stalezone@example.com")
        assert {t.seat_id for t in created} == {seats[3].id, seats[4].id}

    def test_legacy_token_without_the_zone_claim_still_decodes(
        self,
        guest_event_with_tickets: Event,
        user_choice_tier: TicketTier,
        seats: list[VenueSeat],
        existing_guest_user: RevelUser,
    ) -> None:
        """A pre-v3 token whose JSON lacks price_category_id decodes (default None) and confirms."""
        valid_token = guest_service.create_guest_ticket_token(
            existing_guest_user,
            guest_event_with_tickets.id,
            user_choice_tier.id,
            [schema.TicketPurchaseItem(guest_name="Legacy", seat_id=seats[0].id)],
        )
        raw = jwt.decode(
            valid_token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], options={"verify_aud": False}
        )
        raw.pop("price_category_id")  # simulate a token minted before the zone existed
        token = create_token(raw, settings.SECRET_KEY, settings.JWT_ALGORITHM)

        decoded = guest_service.validate_and_decode_guest_token(token)
        assert isinstance(decoded, schema.GuestTicketJWTPayloadSchema)
        assert decoded.price_category_id is None

        response = Client().post(
            reverse("api:confirm_guest_action"), data={"token": token}, content_type="application/json"
        )

        assert response.status_code == 200, response.content
        assert Ticket.objects.get(user=existing_guest_user, event=guest_event_with_tickets).seat == seats[0]
