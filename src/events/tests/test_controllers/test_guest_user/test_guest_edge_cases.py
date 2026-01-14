"""Tests for edge cases, model validation, and error messages."""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import jwt
import pytest
from django.conf import settings
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from accounts.models import RevelUser
from events import schema
from events.models import Event, EventRSVP, Organization, TicketTier
from events.service import guest as guest_service

pytestmark = pytest.mark.django_db


class TestGuestUserEdgeCases:
    """Test edge cases for guest user functionality."""

    @patch("events.tasks.send_guest_rsvp_confirmation.delay")
    def test_empty_first_name(self, mock_send_email: Mock, guest_event: Event) -> None:
        """Test that empty first/last names are handled with 422."""
        # Arrange
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": "noname@example.com",
            "first_name": "",
            "last_name": "",
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 422, response.content

    def test_email_normalization(self, guest_event: Event) -> None:
        """Test that email is normalized to lowercase for case-insensitive matching."""
        # Create user with uppercase email
        user1 = guest_service.get_or_create_guest_user("test@EXAMPLE.com", "Test", "User")

        # Try to create with different case - should return same user
        user2 = guest_service.get_or_create_guest_user("test@example.com", "Test", "User")

        # Email should be normalized to lowercase
        assert user1.email == "test@example.com"
        assert user2.email == "test@example.com"
        # Should be the same user (case-insensitive match)
        assert user1.id == user2.id

    def test_token_expiration_edge_case(self, guest_event: Event, existing_guest_user: RevelUser) -> None:
        """Test token that expires exactly at current time."""
        # Arrange: Create token that expires right now
        now = timezone.now()
        payload = schema.GuestRSVPJWTPayloadSchema(
            user_id=existing_guest_user.id,
            email=existing_guest_user.email,
            event_id=guest_event.id,
            answer="yes",
            exp=now,
            jti="edge-case-jti",
        )
        edge_token = jwt.encode(payload.model_dump(mode="json"), settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

        client = Client()
        url = reverse("api:confirm_guest_action")

        # Act
        response = client.post(url, data={"token": edge_token}, content_type="application/json")

        # Assert - should be treated as expired
        assert response.status_code == 400

    def test_pwyc_amount_exactly_at_min(self, guest_event_with_tickets: Event, pwyc_tier: TicketTier) -> None:
        """Test PWYC amount exactly at minimum is accepted."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_pwyc_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": pwyc_tier.pk},
        )
        payload = {
            "email": "minpwyc@example.com",
            "first_name": "Min",
            "last_name": "PWYC",
            "tickets": [{"guest_name": "Min PWYC"}],
            "price_per_ticket": str(pwyc_tier.pwyc_min),  # Exactly at minimum
        }

        # Act
        with patch("events.tasks.send_guest_ticket_confirmation.delay"):
            response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200

    def test_pwyc_amount_exactly_at_max(self, guest_event_with_tickets: Event, pwyc_tier: TicketTier) -> None:
        """Test PWYC amount exactly at maximum is accepted."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_pwyc_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": pwyc_tier.pk},
        )
        payload = {
            "email": "maxpwyc@example.com",
            "first_name": "Max",
            "last_name": "PWYC",
            "tickets": [{"guest_name": "Max PWYC"}],
            "price_per_ticket": str(pwyc_tier.pwyc_max),  # Exactly at maximum
        }

        # Act
        with patch("events.tasks.send_guest_ticket_confirmation.delay"):
            response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200


class TestGuestModelValidation:
    """Test model field constraints and defaults."""

    def test_revel_user_guest_field_defaults_to_false(self, django_user_model: type[RevelUser]) -> None:
        """Test that RevelUser.guest field defaults to False."""
        user = django_user_model.objects.create_user(
            username="testuser",
            email="testuser@example.com",
            password="pass123",
        )
        assert user.guest is False

    def test_event_can_attend_without_login_defaults_to_true(
        self, organization: Organization, next_week: datetime
    ) -> None:
        """Test that Event.can_attend_without_login field defaults to True."""
        event = Event.objects.create(
            organization=organization,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            start=next_week,
            end=next_week + timedelta(days=1),
        )
        assert event.can_attend_without_login is True

    def test_guest_user_creation_with_guest_true(self, django_user_model: type[RevelUser]) -> None:
        """Test creating a user with guest=True."""
        user = django_user_model.objects.create_user(
            username="guest@test.com",
            email="guest@test.com",
            guest=True,
        )
        assert user.guest is True
        assert user.is_active is True
        assert not user.has_usable_password()

    def test_filtering_users_by_guest_field(
        self, existing_guest_user: RevelUser, existing_regular_user: RevelUser
    ) -> None:
        """Test filtering users by guest field."""
        # Filter for guest users
        guest_users = RevelUser.objects.filter(guest=True)
        assert existing_guest_user in guest_users
        assert existing_regular_user not in guest_users

        # Filter for non-guest users
        regular_users = RevelUser.objects.filter(guest=False)
        assert existing_regular_user in regular_users
        assert existing_guest_user not in regular_users


class TestGuestErrorMessages:
    """Test that error messages are clear and translatable."""

    def test_error_when_authenticated_user_tries_guest_endpoint(
        self, member_client: Client, guest_event: Event
    ) -> None:
        """Test error message includes 'authenticated'."""
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": "auth@example.com",
            "first_name": "Auth",
            "last_name": "User",
        }
        response = member_client.post(url, data=payload, content_type="application/json")

        assert response.status_code == 400
        data = response.json()
        assert "authenticated" in data["detail"].lower()

    def test_error_when_event_requires_login(self, login_required_event: Event) -> None:
        """Test error message includes 'login' or 'requires'."""
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": login_required_event.pk, "answer": "yes"})
        payload = {
            "email": "guest@example.com",
            "first_name": "Guest",
            "last_name": "User",
        }
        response = client.post(url, data=payload, content_type="application/json")

        assert response.status_code == 400
        data = response.json()
        assert "login" in data["detail"].lower() or "requires" in data["detail"].lower()

    def test_error_when_non_guest_email_exists(self, guest_event: Event, existing_regular_user: RevelUser) -> None:
        """Test error message includes 'already exists'."""
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": existing_regular_user.email,
            "first_name": "Different",
            "last_name": "Name",
        }
        response = client.post(url, data=payload, content_type="application/json")

        assert response.status_code == 400
        data = response.json()
        assert "already exists" in data["detail"].lower()

    def test_error_when_token_expired(self, guest_event: Event, existing_guest_user: RevelUser) -> None:
        """Test error message includes 'expired'."""
        # Create expired token
        past_time = timezone.now() - timedelta(hours=2)
        payload = schema.GuestRSVPJWTPayloadSchema(
            user_id=existing_guest_user.id,
            email=existing_guest_user.email,
            event_id=guest_event.id,
            answer="yes",
            exp=past_time,
            jti="expired-jti",
        )
        expired_token = jwt.encode(
            payload.model_dump(mode="json"), settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )

        client = Client()
        url = reverse("api:confirm_guest_action")
        response = client.post(url, data={"token": expired_token}, content_type="application/json")

        assert response.status_code == 400
        data = response.json()
        assert "expired" in data["detail"].lower()

    def test_error_when_event_full(self, guest_event: Event) -> None:
        """Test error message includes 'full' or 'capacity'."""
        # Fill the event
        guest_event.max_attendees = 1
        guest_event.save()

        other_user = RevelUser.objects.create_user(
            username="filler@example.com",
            email="filler@example.com",
            guest=True,
        )
        EventRSVP.objects.create(event=guest_event, user=other_user, status=EventRSVP.RsvpStatus.YES)

        # Try to RSVP as guest
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": "late@example.com",
            "first_name": "Late",
            "last_name": "User",
        }
        response = client.post(url, data=payload, content_type="application/json")

        assert response.status_code == 400
        data = response.json()
        assert "full" in data["reason"].lower()
