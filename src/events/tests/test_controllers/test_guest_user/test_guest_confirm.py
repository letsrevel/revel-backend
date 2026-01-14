"""Tests for guest action confirmation: POST /events/guest-actions/confirm."""

from datetime import timedelta

import jwt
import pytest
from django.conf import settings
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.token_blacklist.models import BlacklistedToken

from accounts.models import RevelUser
from events import schema
from events.models import Event, EventRSVP, Ticket, TicketTier
from events.service import guest as guest_service

pytestmark = pytest.mark.django_db


class TestConfirmGuestAction:
    """Test guest action confirmation endpoint."""

    def test_confirm_guest_rsvp_success(self, guest_event: Event, existing_guest_user: RevelUser) -> None:
        """Test successful RSVP confirmation via token."""
        # Arrange
        token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "yes")

        client = Client()
        url = reverse("api:confirm_guest_action")
        payload = {"token": token}

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "yes"
        assert data["event_id"] == str(guest_event.id)

        # Verify RSVP was created
        rsvp = EventRSVP.objects.get(user=existing_guest_user, event=guest_event)
        assert rsvp.status == EventRSVP.RsvpStatus.YES

        # Verify token was blacklisted
        payload_decoded = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
        assert BlacklistedToken.objects.filter(token__jti=payload_decoded["jti"]).exists()

    def test_confirm_guest_ticket_success(
        self, guest_event_with_tickets: Event, free_tier: TicketTier, existing_guest_user: RevelUser
    ) -> None:
        """Test successful ticket confirmation via token."""
        # Arrange
        tickets = [schema.TicketPurchaseItem(guest_name="Test Guest")]
        token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, free_tier.id, tickets
        )

        client = Client()
        url = reverse("api:confirm_guest_action")
        payload = {"token": token}

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert - BatchCheckoutResponse for consistency
        assert response.status_code == 200
        data = response.json()
        assert "tickets" in data
        assert len(data["tickets"]) == 1
        assert data["tickets"][0]["tier"]["name"] == free_tier.name

        # Verify ticket was created
        ticket = Ticket.objects.get(user=existing_guest_user, event=guest_event_with_tickets)
        assert ticket.tier == free_tier
        assert ticket.status == Ticket.TicketStatus.ACTIVE
        assert ticket.guest_name == "Test Guest"

    def test_confirm_guest_action_rejects_expired_token(
        self, guest_event: Event, existing_guest_user: RevelUser
    ) -> None:
        """Test that expired tokens are rejected."""
        # Arrange: Create a token that expired 2 hours ago
        past_time = timezone.now() - timedelta(hours=2)
        payload = schema.GuestRSVPJWTPayloadSchema(
            user_id=existing_guest_user.id,
            email=existing_guest_user.email,
            event_id=guest_event.id,
            answer="yes",
            exp=past_time,
            jti="expired-jti-12345",
        )
        expired_token = jwt.encode(
            payload.model_dump(mode="json"), settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )

        client = Client()
        url = reverse("api:confirm_guest_action")
        payload_data = {"token": expired_token}

        # Act
        response = client.post(url, data=payload_data, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "expired" in data["detail"].lower()

    def test_confirm_guest_action_rejects_blacklisted_token(
        self, guest_event: Event, existing_guest_user: RevelUser
    ) -> None:
        """Test that blacklisted tokens are rejected (replay attack prevention)."""
        # Arrange
        token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "yes")

        # Use the token once
        client = Client()
        url = reverse("api:confirm_guest_action")
        payload = {"token": token}
        response = client.post(url, data=payload, content_type="application/json")
        assert response.status_code == 200

        # Try to use it again
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 401  # check_blacklist raises 401
        data = response.json()
        assert "blacklist" in data["detail"].lower()

    def test_confirm_guest_action_rejects_invalid_token(self) -> None:
        """Test that invalid tokens are rejected."""
        # Arrange
        client = Client()
        url = reverse("api:confirm_guest_action")
        payload = {"token": "invalid.jwt.token"}

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "invalid" in data["detail"].lower()

    def test_confirm_guest_action_handles_discriminated_union(
        self, guest_event: Event, guest_event_with_tickets: Event, free_tier: TicketTier, existing_guest_user: RevelUser
    ) -> None:
        """Test that discriminated union correctly routes to RSVP vs ticket handlers."""
        # Arrange: Create both types of tokens
        rsvp_token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "maybe")
        tickets = [schema.TicketPurchaseItem(guest_name="Ticket Guest")]
        ticket_token = guest_service.create_guest_ticket_token(
            existing_guest_user, guest_event_with_tickets.id, free_tier.id, tickets
        )

        client = Client()
        url = reverse("api:confirm_guest_action")

        # Act & Assert: RSVP token
        response = client.post(url, data={"token": rsvp_token}, content_type="application/json")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data  # RSVP schema
        assert data["status"] == "maybe"

        # Act & Assert: Ticket token
        response = client.post(url, data={"token": ticket_token}, content_type="application/json")
        assert response.status_code == 200
        data = response.json()
        assert "tickets" in data  # BatchCheckoutResponse schema

    def test_confirm_guest_action_rechecks_eligibility(
        self, guest_event: Event, existing_guest_user: RevelUser
    ) -> None:
        """Test that eligibility is rechecked at confirmation time."""
        # Arrange: Create token when event has capacity
        token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "yes")

        # Fill the event to capacity
        guest_event.max_attendees = 1
        guest_event.save()
        other_user = RevelUser.objects.create_user(
            username="filler@example.com",
            email="filler@example.com",
            guest=True,
        )
        EventRSVP.objects.create(event=guest_event, user=other_user, status=EventRSVP.RsvpStatus.YES)

        client = Client()
        url = reverse("api:confirm_guest_action")
        payload = {"token": token}

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "full" in data["reason"].lower()

        # Token should NOT be blacklisted since action failed
        payload_decoded = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
        assert not BlacklistedToken.objects.filter(token__jti=payload_decoded["jti"]).exists()
