"""Tests for guest service layer functions."""

from decimal import Decimal
from unittest.mock import Mock, patch

import jwt
import pytest
from django.conf import settings
from ninja_jwt.token_blacklist.models import BlacklistedToken

from accounts.models import RevelUser
from events import schema
from events.models import Event, EventRSVP, TicketTier
from events.service import guest as guest_service
from events.service.event_manager import UserIsIneligibleError

pytestmark = pytest.mark.django_db


class TestGuestServiceLayer:
    """Test guest service layer functions."""

    def test_get_or_create_guest_user_creates_new(self) -> None:
        """Test creating a new guest user."""
        # Act
        user = guest_service.get_or_create_guest_user("newuser@test.com", "New", "User")

        # Assert
        assert user.email == "newuser@test.com"
        assert user.first_name == "New"
        assert user.last_name == "User"
        assert user.guest is True
        assert user.is_active is True
        assert user.email_verified is False
        assert not user.has_usable_password()

    def test_get_or_create_guest_user_updates_existing_guest(self, existing_guest_user: RevelUser) -> None:
        """Test updating an existing guest user's name."""
        # Act
        user = guest_service.get_or_create_guest_user(existing_guest_user.email, "Updated", "Name")

        # Assert
        assert user.id == existing_guest_user.id
        assert user.first_name == "Updated"
        assert user.last_name == "Name"

    def test_get_or_create_guest_user_rejects_non_guest(self, existing_regular_user: RevelUser) -> None:
        """Test that attempting to create guest with existing non-guest email fails."""
        # Act & Assert
        with pytest.raises(Exception) as exc_info:
            guest_service.get_or_create_guest_user(existing_regular_user.email, "New", "Name")

        assert "already exists" in str(exc_info.value).lower()

    def test_create_guest_rsvp_token(self, existing_guest_user: RevelUser, guest_event: Event) -> None:
        """Test creating a guest RSVP JWT token."""
        # Act
        token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "yes")

        # Assert
        assert isinstance(token, str)

        # Decode and verify
        payload_decoded = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
        assert payload_decoded["type"] == "guest_rsvp"
        assert payload_decoded["user_id"] == str(existing_guest_user.id)
        assert payload_decoded["email"] == existing_guest_user.email
        assert payload_decoded["event_id"] == str(guest_event.id)
        assert payload_decoded["answer"] == "yes"
        assert "jti" in payload_decoded
        assert "exp" in payload_decoded

    def test_create_guest_ticket_token(
        self, existing_guest_user: RevelUser, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test creating a guest ticket JWT token."""
        # Act
        tickets = [schema.TicketPurchaseItem(guest_name="Test Guest")]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, free_tier.id, tickets
        )

        # Assert
        assert isinstance(token, str)

        # Decode and verify
        payload_decoded = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
        assert payload_decoded["type"] == "guest_ticket"
        assert payload_decoded["user_id"] == str(existing_guest_user.id)
        assert payload_decoded["email"] == existing_guest_user.email
        assert payload_decoded["event_id"] == str(guest_event_with_tickets.id)
        assert payload_decoded["tier_id"] == str(free_tier.id)
        assert payload_decoded["pwyc_amount"] is None
        assert len(payload_decoded["tickets"]) == 1
        assert payload_decoded["tickets"][0]["guest_name"] == "Test Guest"

    def test_create_guest_ticket_token_with_pwyc_amount(
        self, existing_guest_user: RevelUser, guest_event_with_tickets: Event, pwyc_tier: TicketTier
    ) -> None:
        """Test creating a guest ticket JWT token with PWYC amount."""
        # Act
        pwyc_amount = Decimal("15.00")
        tickets = [schema.TicketPurchaseItem(guest_name="PWYC Guest")]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, pwyc_tier.id, tickets, pwyc_amount
        )

        # Assert
        payload_decoded = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
        assert payload_decoded["pwyc_amount"] == "15.00"
        assert len(payload_decoded["tickets"]) == 1

    def test_validate_and_decode_guest_token_rsvp(self, existing_guest_user: RevelUser, guest_event: Event) -> None:
        """Test validating and decoding a guest RSVP token."""
        # Arrange
        token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "no")

        # Act
        payload = guest_service.validate_and_decode_guest_token(token)

        # Assert
        assert isinstance(payload, schema.GuestRSVPJWTPayloadSchema)
        assert payload.user_id == existing_guest_user.id
        assert payload.event_id == guest_event.id
        assert payload.answer == "no"

    def test_validate_and_decode_guest_token_ticket(
        self, existing_guest_user: RevelUser, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test validating and decoding a guest ticket token."""
        # Arrange
        tickets = [schema.TicketPurchaseItem(guest_name="Test Guest")]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, free_tier.id, tickets
        )

        # Act
        payload = guest_service.validate_and_decode_guest_token(token)

        # Assert
        assert isinstance(payload, schema.GuestTicketJWTPayloadSchema)
        assert payload.user_id == existing_guest_user.id
        assert payload.event_id == guest_event_with_tickets.id
        assert payload.tier_id == free_tier.id
        assert len(payload.tickets) == 1
        assert payload.tickets[0].guest_name == "Test Guest"

    @patch("events.tasks.send_guest_rsvp_confirmation.delay")
    def test_handle_guest_rsvp(self, mock_send_email: Mock, guest_event: Event) -> None:
        """Test handle_guest_rsvp service function."""
        # Act
        result = guest_service.handle_guest_rsvp(
            guest_event, EventRSVP.RsvpStatus.YES, "service@test.com", "Service", "Test"
        )

        # Assert
        assert isinstance(result, schema.GuestActionResponseSchema)
        assert "email" in result.message.lower()

        # Verify user was created
        user = RevelUser.objects.get(email="service@test.com")
        assert user.guest is True

        # Verify email was sent
        mock_send_email.assert_called_once()

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_handle_guest_ticket_checkout_offline(
        self, mock_send_email: Mock, guest_event_with_tickets: Event, offline_tier: TicketTier
    ) -> None:
        """Test handle_guest_ticket_checkout for offline payment."""
        # Act
        tickets = [schema.TicketPurchaseItem(guest_name="Offline Test")]
        result = guest_service.handle_guest_ticket_checkout(
            guest_event_with_tickets, offline_tier, "offline@test.com", "Offline", "Test", tickets
        )

        # Assert
        assert isinstance(result, schema.GuestCheckoutResponseSchema)
        assert result.message is not None
        assert result.checkout_url is None
        mock_send_email.assert_called_once()

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_handle_guest_ticket_checkout_online(
        self, mock_stripe: Mock, guest_event_with_tickets: Event, online_tier: TicketTier
    ) -> None:
        """Test handle_guest_ticket_checkout for online payment."""
        # Arrange
        checkout_url = "https://checkout.stripe.com/test"
        mock_stripe.return_value = checkout_url

        # Act
        tickets = [schema.TicketPurchaseItem(guest_name="Stripe Test")]
        result = guest_service.handle_guest_ticket_checkout(
            guest_event_with_tickets, online_tier, "stripe@test.com", "Stripe", "Test", tickets
        )

        # Assert
        assert isinstance(result, schema.GuestCheckoutResponseSchema)
        assert result.checkout_url == checkout_url
        assert result.message is None

    def test_handle_guest_ticket_checkout_validates_pwyc_min(
        self, guest_event_with_tickets: Event, pwyc_tier: TicketTier
    ) -> None:
        """Test that PWYC amount validation works in service layer."""
        # Act & Assert
        tickets = [schema.TicketPurchaseItem(guest_name="Test User")]
        with pytest.raises(Exception) as exc_info:
            guest_service.handle_guest_ticket_checkout(
                guest_event_with_tickets,
                pwyc_tier,
                "test@test.com",
                "Test",
                "User",
                tickets,
                pwyc_amount=Decimal("1.00"),  # Below min of 5.00
            )
        assert "at least" in str(exc_info.value).lower()

    def test_handle_guest_ticket_checkout_validates_pwyc_max(
        self, guest_event_with_tickets: Event, pwyc_tier: TicketTier
    ) -> None:
        """Test that PWYC max validation works in service layer."""
        # Act & Assert
        tickets = [schema.TicketPurchaseItem(guest_name="Test User")]
        with pytest.raises(Exception) as exc_info:
            guest_service.handle_guest_ticket_checkout(
                guest_event_with_tickets,
                pwyc_tier,
                "test@test.com",
                "Test",
                "User",
                tickets,
                pwyc_amount=Decimal("100.00"),  # Above max of 50.00
            )
        assert "at most" in str(exc_info.value).lower()

    def test_confirm_guest_action_atomic_transaction(self, guest_event: Event, existing_guest_user: RevelUser) -> None:
        """Test that confirm_guest_action is atomic (token blacklisted only on success)."""
        # Arrange: Create token for an event that will fail capacity check
        guest_event.max_attendees = 1
        guest_event.save()

        token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "yes")
        EventRSVP.objects.create(user=existing_guest_user, event=guest_event, status="yes")

        # Act & Assert
        with pytest.raises(UserIsIneligibleError):
            guest_service.confirm_guest_action(token)

        # Token should NOT be blacklisted since transaction rolled back
        payload_decoded = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
        assert not BlacklistedToken.objects.filter(token__jti=payload_decoded["jti"]).exists()
