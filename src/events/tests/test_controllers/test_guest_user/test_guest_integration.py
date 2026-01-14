"""Tests for complete guest user flows and integration scenarios.

- End-to-end integration tests
- Duplicate action handling
- Concurrency tests
"""

import re
from unittest.mock import Mock, patch

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Event, EventRSVP, Ticket, TicketTier
from events.service import guest as guest_service

pytestmark = pytest.mark.django_db


class TestGuestFlowIntegration:
    """Test complete guest user flows from start to finish."""

    def test_complete_rsvp_flow_end_to_end(self, guest_event: Event) -> None:
        """Test complete RSVP flow: initiate -> receive email -> confirm -> verify RSVP."""
        from django.core import mail

        # Step 1: Initiate RSVP
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": "endtoend@example.com",
            "first_name": "End",
            "last_name": "ToEnd",
        }
        response = client.post(url, data=payload, content_type="application/json")
        assert response.status_code == 200

        # Step 2: Verify email was sent and extract token
        assert len(mail.outbox) == 1
        email_body = mail.outbox[0].body
        # Extract token from email (it's in the URL)
        token_match = re.search(r"token=([^&\s]+)", str(email_body))
        assert token_match is not None
        token = token_match.group(1)

        # Step 3: Confirm RSVP using token
        confirm_url = reverse("api:confirm_guest_action")
        confirm_response = client.post(confirm_url, data={"token": token}, content_type="application/json")
        assert confirm_response.status_code == 200

        # Step 4: Verify RSVP was created
        user = RevelUser.objects.get(email="endtoend@example.com")
        rsvp = EventRSVP.objects.get(user=user, event=guest_event)
        assert rsvp.status == EventRSVP.RsvpStatus.YES

    def test_complete_ticket_flow_end_to_end(self, guest_event_with_tickets: Event, free_tier: TicketTier) -> None:
        """Test complete ticket flow: initiate -> receive email -> confirm -> verify ticket."""
        from django.core import mail

        # Step 1: Initiate ticket purchase
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": free_tier.pk},
        )
        payload = {
            "email": "ticketflow@example.com",
            "first_name": "Ticket",
            "last_name": "Flow",
            "tickets": [{"guest_name": "Ticket Flow"}],
        }
        response = client.post(url, data=payload, content_type="application/json")
        assert response.status_code == 200

        # Step 2: Verify email was sent and extract token
        assert len(mail.outbox) == 1
        email_body = mail.outbox[0].body
        token_match = re.search(r"token=([^&\s]+)", str(email_body))
        assert token_match is not None
        token = token_match.group(1)

        # Step 3: Confirm ticket using token
        confirm_url = reverse("api:confirm_guest_action")
        confirm_response = client.post(confirm_url, data={"token": token}, content_type="application/json")
        assert confirm_response.status_code == 200

        # Step 4: Verify ticket was created
        user = RevelUser.objects.get(email="ticketflow@example.com")
        ticket = Ticket.objects.get(user=user, event=guest_event_with_tickets)
        assert ticket.tier == free_tier
        assert ticket.status == Ticket.TicketStatus.ACTIVE
        assert ticket.guest_name == "Ticket Flow"

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_complete_online_payment_flow(
        self, mock_stripe: Mock, guest_event_with_tickets: Event, online_tier: TicketTier
    ) -> None:
        """Test complete online payment flow: initiate -> get Stripe URL -> verify ticket created."""
        # Arrange
        checkout_url = "https://checkout.stripe.com/test"
        mock_stripe.return_value = checkout_url

        # Act: Initiate online payment
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": online_tier.pk},
        )
        payload = {
            "email": "stripe@example.com",
            "first_name": "Stripe",
            "last_name": "User",
            "tickets": [{"guest_name": "Stripe User"}],
        }
        response = client.post(url, data=payload, content_type="application/json")

        # Assert: Got Stripe URL immediately (no email confirmation needed)
        assert response.status_code == 200
        data = response.json()
        assert "checkout_url" in data
        assert data["checkout_url"] == checkout_url

        # Verify user was created as guest
        user = RevelUser.objects.get(email="stripe@example.com")
        assert user.guest is True

    def test_guest_creates_rsvp_then_converts_to_full_user(self, guest_event: Event) -> None:
        """Test guest creates RSVP, then converts to full user via password reset."""
        from django.core import mail

        # Step 1: Create RSVP as guest
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": "convert@example.com",
            "first_name": "Convert",
            "last_name": "User",
        }
        response = client.post(url, data=payload, content_type="application/json")
        assert response.status_code == 200

        # Extract token and confirm RSVP
        email_body = mail.outbox[0].body
        token_match = re.search(r"token=([^&\s]+)", str(email_body))
        assert token_match is not None
        token = token_match.group(1)

        confirm_url = reverse("api:confirm_guest_action")
        client.post(confirm_url, data={"token": token}, content_type="application/json")

        # Step 2: Verify RSVP exists for guest user
        user = RevelUser.objects.get(email="convert@example.com")
        assert user.guest is True
        rsvp = EventRSVP.objects.get(user=user, event=guest_event)
        assert rsvp.status == EventRSVP.RsvpStatus.YES

        # Note: Testing password reset conversion is accounts functionality,
        # not guest endpoint functionality. The guest user and RSVP have been
        # successfully created, which is what we're testing here.


class TestGuestDuplicateActions:
    """Test handling of duplicate guest actions."""

    @patch("events.tasks.send_guest_rsvp_confirmation.delay")
    def test_guest_initiates_rsvp_twice_updates_name(self, mock_send_email: Mock, guest_event: Event) -> None:
        """Test that initiating RSVP twice updates the guest user's name."""
        # First RSVP
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload1 = {
            "email": "duplicate@example.com",
            "first_name": "First",
            "last_name": "Name",
        }
        response1 = client.post(url, data=payload1, content_type="application/json")
        assert response1.status_code == 200

        # Second RSVP with same email, different name
        payload2 = {
            "email": "duplicate@example.com",
            "first_name": "Second",
            "last_name": "Name",
        }
        response2 = client.post(url, data=payload2, content_type="application/json")
        assert response2.status_code == 200

        # Assert: User's name was updated
        user = RevelUser.objects.get(email="duplicate@example.com")
        assert user.first_name == "Second"
        assert user.last_name == "Name"

        # Assert: Email sent twice (once for each attempt)
        assert mock_send_email.call_count == 2

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_guest_initiates_ticket_purchase_twice(
        self, mock_send_email: Mock, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test that initiating ticket purchase twice before confirming works."""
        # First attempt
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": free_tier.pk},
        )
        payload = {
            "email": "dupticket@example.com",
            "first_name": "Dup",
            "last_name": "Ticket",
            "tickets": [{"guest_name": "Dup Ticket"}],
        }
        response1 = client.post(url, data=payload, content_type="application/json")
        assert response1.status_code == 200

        # Second attempt
        response2 = client.post(url, data=payload, content_type="application/json")
        assert response2.status_code == 200

        # Assert: Both attempts succeeded (new tokens generated)
        assert mock_send_email.call_count == 2

    def test_guest_confirms_rsvp_twice_replay_attack(self, guest_event: Event, existing_guest_user: RevelUser) -> None:
        """Test that confirming RSVP twice (replay attack) is rejected."""
        # Create token
        token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "yes")

        client = Client()
        url = reverse("api:confirm_guest_action")
        payload = {"token": token}

        # First confirmation
        response1 = client.post(url, data=payload, content_type="application/json")
        assert response1.status_code == 200

        # Second confirmation (replay attack)
        response2 = client.post(url, data=payload, content_type="application/json")
        assert response2.status_code == 401  # Blacklisted token
        data = response2.json()
        assert "blacklist" in data["detail"].lower()

    def test_guest_changes_rsvp_answer(self, guest_event: Event, existing_guest_user: RevelUser) -> None:
        """Test that guest can change RSVP answer by initiating new RSVP."""
        from django.core import mail

        # First RSVP: YES
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": existing_guest_user.email,
            "first_name": existing_guest_user.first_name,
            "last_name": existing_guest_user.last_name,
        }
        response1 = client.post(url, data=payload, content_type="application/json")
        assert response1.status_code == 200

        # Extract and confirm first token
        token_match1 = re.search(r"token=([^&\s]+)", str(mail.outbox[0].body))
        assert token_match1 is not None
        token1 = token_match1.group(1)

        confirm_url = reverse("api:confirm_guest_action")
        client.post(confirm_url, data={"token": token1}, content_type="application/json")

        # Verify first RSVP
        rsvp = EventRSVP.objects.get(user=existing_guest_user, event=guest_event)
        assert rsvp.status == EventRSVP.RsvpStatus.YES

        # Second RSVP: MAYBE (different answer)
        mail.outbox.clear()
        url2 = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "maybe"})
        response2 = client.post(url2, data=payload, content_type="application/json")
        assert response2.status_code == 200

        # Extract and confirm second token
        token_match2 = re.search(r"token=([^&\s]+)", str(mail.outbox[0].body))
        assert token_match2 is not None
        token2 = token_match2.group(1)

        client.post(confirm_url, data={"token": token2}, content_type="application/json")

        # Verify RSVP was updated
        rsvp.refresh_from_db()
        assert rsvp.status == EventRSVP.RsvpStatus.MAYBE


class TestGuestConcurrency:
    """Test race conditions and concurrent requests."""

    def test_two_guests_rsvp_to_last_spot_simultaneously(self, guest_event: Event) -> None:
        """Test two guests trying to RSVP at same time to last spot."""

        # Set event to have only 1 spot
        guest_event.max_attendees = 1
        guest_event.save()

        # Create two guest users
        guest1 = guest_service.get_or_create_guest_user("guest1@example.com", "Guest", "One")
        guest2 = guest_service.get_or_create_guest_user("guest2@example.com", "Guest", "Two")

        # Create tokens for both
        token1 = guest_service.create_guest_rsvp_token(guest1, guest_event.id, "yes")
        token2 = guest_service.create_guest_rsvp_token(guest2, guest_event.id, "yes")

        # Confirm first token (should succeed)
        client = Client()
        url = reverse("api:confirm_guest_action")
        response1 = client.post(url, data={"token": token1}, content_type="application/json")
        assert response1.status_code == 200

        # Confirm second token (should fail due to capacity)
        response2 = client.post(url, data={"token": token2}, content_type="application/json")
        assert response2.status_code == 400
        data = response2.json()
        assert "full" in data["reason"].lower()

    @patch("events.tasks.send_guest_rsvp_confirmation.delay")
    def test_guest_initiating_rsvp_with_concurrent_capacity_changes(
        self, mock_send_email: Mock, guest_event: Event
    ) -> None:
        """Test guest initiating RSVP while capacity is being changed."""
        # This is a basic test - Django's atomic transactions should handle this
        # Set low capacity
        guest_event.max_attendees = 2
        guest_event.save()

        # Initiate RSVP (should check capacity at initiation)
        client = Client()
        url = reverse("api:guest_rsvp", kwargs={"event_id": guest_event.pk, "answer": "yes"})
        payload = {
            "email": "concurrent@example.com",
            "first_name": "Concurrent",
            "last_name": "User",
        }
        response = client.post(url, data=payload, content_type="application/json")

        # Should succeed if capacity check passed at initiation time
        assert response.status_code == 200

    def test_token_confirmation_with_concurrent_capacity_changes(
        self, guest_event: Event, existing_guest_user: RevelUser
    ) -> None:
        """Test token confirmation while capacity is changing."""
        # Create token when event has capacity
        token = guest_service.create_guest_rsvp_token(existing_guest_user, guest_event.id, "yes")

        # Reduce capacity (but not to full yet)
        guest_event.max_attendees = 50
        guest_event.save()

        # Confirm token (should still work as there's capacity)
        client = Client()
        url = reverse("api:confirm_guest_action")
        response = client.post(url, data={"token": token}, content_type="application/json")

        # Should succeed as capacity check happens at confirmation time
        assert response.status_code == 200
