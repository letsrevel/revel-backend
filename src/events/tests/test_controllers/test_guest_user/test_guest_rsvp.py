"""Tests for guest RSVP endpoint: POST /events/{event_id}/rsvp/{answer}/public."""

from unittest.mock import Mock, patch

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Event, EventRSVP

pytestmark = pytest.mark.django_db


class TestGuestRSVP:
    """Test guest RSVP endpoint."""

    @patch("events.tasks.send_guest_rsvp_confirmation.delay")
    def test_guest_rsvp_success(self, mock_send_email: Mock, guest_event: Event) -> None:
        """Test successful guest RSVP initiates email confirmation."""
        # Arrange
        client = Client()  # Unauthenticated client
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": "newguest@example.com",
            "first_name": "New",
            "last_name": "Guest",
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "email" in data["message"].lower() or "confirm" in data["message"].lower()

        # Check guest user was created
        guest_user = RevelUser.objects.get(email="newguest@example.com")
        assert guest_user.guest is True
        assert guest_user.first_name == "New"
        assert guest_user.last_name == "Guest"

        # Check email was sent
        mock_send_email.assert_called_once()
        call_args = mock_send_email.call_args[0]
        assert call_args[0] == "newguest@example.com"  # email
        assert call_args[2] == guest_event.name  # event_name
        # Token is the second argument
        token = call_args[1]
        assert isinstance(token, str)

        # Verify RSVP was NOT created yet (pending email confirmation)
        assert not EventRSVP.objects.filter(user=guest_user, event=guest_event).exists()

    def test_guest_rsvp_rejects_authenticated_user(self, member_client: Client, guest_event: Event) -> None:
        """Test that authenticated users cannot use guest RSVP endpoint."""
        # Arrange
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": "authenticated@example.com",
            "first_name": "Auth",
            "last_name": "User",
        }

        # Act
        response = member_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "authenticated" in data["detail"].lower()

    def test_guest_rsvp_rejects_login_required_event(self, login_required_event: Event) -> None:
        """Test that guest RSVP is rejected if event requires login."""
        # Arrange
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": login_required_event.pk, "answer": "yes"})
        payload = {
            "email": "guest@example.com",
            "first_name": "Guest",
            "last_name": "User",
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "login" in data["detail"].lower() or "requires login" in data["detail"].lower()

    def test_guest_rsvp_rejects_existing_non_guest_email(
        self, guest_event: Event, existing_regular_user: RevelUser
    ) -> None:
        """Test that guest RSVP is rejected if non-guest account exists with email."""
        # Arrange
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": existing_regular_user.email,
            "first_name": "Different",
            "last_name": "Name",
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "already exists" in data["detail"].lower() or "log in" in data["detail"].lower()

    @patch("events.tasks.send_guest_rsvp_confirmation.delay")
    def test_guest_rsvp_updates_existing_guest_user(
        self, mock_send_email: Mock, guest_event: Event, existing_guest_user: RevelUser
    ) -> None:
        """Test that guest RSVP updates name for existing guest users."""
        # Arrange
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": existing_guest_user.email,
            "first_name": "Updated",
            "last_name": "Name",
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        existing_guest_user.refresh_from_db()
        assert existing_guest_user.first_name == "Updated"
        assert existing_guest_user.last_name == "Name"
        mock_send_email.assert_called_once()

    def test_guest_rsvp_checks_event_capacity(self, guest_event: Event) -> None:
        """Test that guest RSVP respects event capacity limits."""
        # Arrange
        guest_event.max_attendees = 1
        guest_event.save()

        # Fill the event
        other_user = RevelUser.objects.create_user(
            username="other@example.com",
            email="other@example.com",
            guest=True,
        )
        EventRSVP.objects.create(event=guest_event, user=other_user, status=EventRSVP.RsvpStatus.YES)

        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": "lateguest@example.com",
            "first_name": "Late",
            "last_name": "Guest",
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        # Eligibility check should fail
        assert "full" in data["reason"].lower()
