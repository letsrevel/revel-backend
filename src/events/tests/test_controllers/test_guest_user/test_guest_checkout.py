"""Tests for guest ticket checkout endpoints.

- Fixed-price: POST /events/{event_id}/tickets/{tier_id}/checkout/public
- PWYC: POST /events/{event_id}/tickets/{tier_id}/checkout/pwyc/public
"""

from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketTier

pytestmark = pytest.mark.django_db


class TestGuestTicketCheckout:
    """Test guest ticket checkout endpoint for fixed-price tiers."""

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_guest_checkout_free_ticket_sends_confirmation(
        self, mock_send_email: Mock, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test guest checkout for free ticket sends email confirmation."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": free_tier.pk},
        )
        payload = {
            "email": "freeticket@example.com",
            "first_name": "Free",
            "last_name": "Ticket",
            "tickets": [{"guest_name": "Free Ticket"}],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "email" in data["message"].lower()

        # Check guest user was created
        guest_user = RevelUser.objects.get(email="freeticket@example.com")
        assert guest_user.guest is True

        # Check email was sent
        mock_send_email.assert_called_once()
        call_args = mock_send_email.call_args[0]
        assert call_args[0] == "freeticket@example.com"
        assert call_args[2] == guest_event_with_tickets.name
        assert call_args[3] == free_tier.name

        # Verify ticket was NOT created yet
        assert not Ticket.objects.filter(user=guest_user, event=guest_event_with_tickets).exists()

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_guest_checkout_online_payment_returns_stripe_url(
        self,
        mock_stripe: Mock,
        guest_event_with_tickets: Event,
        online_tier: TicketTier,
    ) -> None:
        """Test guest checkout for online payment returns Stripe URL immediately."""
        # Arrange
        checkout_url = "https://checkout.stripe.com/pay/cs_test123"
        mock_stripe.return_value = checkout_url

        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": online_tier.pk},
        )
        payload = {
            "email": "onlineguest@example.com",
            "first_name": "Online",
            "last_name": "Guest",
            "tickets": [{"guest_name": "Online Guest"}],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "checkout_url" in data
        assert data["checkout_url"] == checkout_url

        # Check guest user was created
        guest_user = RevelUser.objects.get(email="onlineguest@example.com")
        assert guest_user.guest is True

        # Verify Stripe was called
        mock_stripe.assert_called_once()

    def test_guest_checkout_rejects_authenticated_user(
        self, member_client: Client, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test that authenticated users cannot use guest checkout."""
        # Arrange
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": free_tier.pk},
        )
        payload = {
            "email": "auth@example.com",
            "first_name": "Auth",
            "last_name": "User",
            "tickets": [{"guest_name": "Auth User"}],
        }

        # Act
        response = member_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "authenticated" in data["detail"].lower()

    def test_guest_checkout_rejects_login_required_event(self, login_required_event: Event) -> None:
        """Test that guest checkout is rejected if event requires login."""
        # Create a tier for login-required event
        tier = TicketTier.objects.create(
            event=login_required_event,
            name="Test Tier",
            price=Decimal("0.00"),
            payment_method=TicketTier.PaymentMethod.FREE,
        )

        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": login_required_event.pk, "tier_id": tier.pk},
        )
        payload = {
            "email": "guest@example.com",
            "first_name": "Guest",
            "last_name": "User",
            "tickets": [{"guest_name": "Guest User"}],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "login" in data["detail"].lower()

    def test_guest_checkout_rejects_existing_non_guest_email(
        self, guest_event_with_tickets: Event, free_tier: TicketTier, existing_regular_user: RevelUser
    ) -> None:
        """Test that guest checkout is rejected if non-guest account exists."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": free_tier.pk},
        )
        payload = {
            "email": existing_regular_user.email,
            "first_name": "Different",
            "last_name": "Name",
            "tickets": [{"guest_name": "Different Name"}],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "already exists" in data["detail"].lower()

    def test_guest_checkout_rejects_pwyc_tier(self, guest_event_with_tickets: Event, pwyc_tier: TicketTier) -> None:
        """Test that PWYC tiers must use the /pwyc endpoint."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": pwyc_tier.pk},
        )
        payload = {
            "email": "guest@example.com",
            "first_name": "Guest",
            "last_name": "User",
            "tickets": [{"guest_name": "Guest User"}],
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "pwyc" in data["detail"].lower()


class TestGuestPWYCCheckout:
    """Test guest PWYC ticket checkout endpoint."""

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_guest_pwyc_checkout_offline_success(
        self, mock_send_email: Mock, guest_event_with_tickets: Event, pwyc_tier: TicketTier
    ) -> None:
        """Test successful guest PWYC checkout with offline payment."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_pwyc_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": pwyc_tier.pk},
        )
        payload = {
            "email": "pwycguest@example.com",
            "first_name": "PWYC",
            "last_name": "Guest",
            "tickets": [{"guest_name": "PWYC Guest"}],
            "price_per_ticket": "15.00",
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "message" in data

        mock_send_email.assert_called_once()

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_guest_pwyc_checkout_online_success(
        self,
        mock_stripe: Mock,
        guest_event_with_tickets: Event,
        pwyc_online_tier: TicketTier,
    ) -> None:
        """Test successful guest PWYC checkout with online payment."""
        # Arrange
        checkout_url = "https://checkout.stripe.com/pay/cs_test123"
        mock_stripe.return_value = checkout_url

        client = Client()
        url = reverse(
            "api:guest_ticket_pwyc_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": pwyc_online_tier.pk},
        )
        payload = {
            "email": "pwycstripe@example.com",
            "first_name": "PWYC",
            "last_name": "Stripe",
            "tickets": [{"guest_name": "PWYC Stripe"}],
            "price_per_ticket": "25.00",
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "checkout_url" in data
        assert data["checkout_url"] == checkout_url

    def test_guest_pwyc_checkout_rejects_amount_below_min(
        self, guest_event_with_tickets: Event, pwyc_tier: TicketTier
    ) -> None:
        """Test that PWYC amount below minimum is rejected."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_pwyc_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": pwyc_tier.pk},
        )
        payload = {
            "email": "lowpwyc@example.com",
            "first_name": "Low",
            "last_name": "PWYC",
            "tickets": [{"guest_name": "Low PWYC"}],
            "price_per_ticket": "2.00",  # Below pwyc_min of 5.00
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "at least" in data["detail"].lower() or "minimum" in data["detail"].lower()

    def test_guest_pwyc_checkout_rejects_amount_above_max(
        self, guest_event_with_tickets: Event, pwyc_tier: TicketTier
    ) -> None:
        """Test that PWYC amount above maximum is rejected."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_pwyc_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": pwyc_tier.pk},
        )
        payload = {
            "email": "highpwyc@example.com",
            "first_name": "High",
            "last_name": "PWYC",
            "tickets": [{"guest_name": "High PWYC"}],
            "price_per_ticket": "100.00",  # Above pwyc_max of 50.00
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "at most" in data["detail"].lower() or "maximum" in data["detail"].lower()

    def test_guest_pwyc_checkout_rejects_non_pwyc_tier(
        self, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test that non-PWYC tiers are rejected."""
        # Arrange
        client = Client()
        url = reverse(
            "api:guest_ticket_pwyc_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": free_tier.pk},
        )
        payload = {
            "email": "guest@example.com",
            "first_name": "Guest",
            "last_name": "User",
            "tickets": [{"guest_name": "Guest User"}],
            "price_per_ticket": "10.00",
        }

        # Act
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "pay-what-you-can" in data["detail"].lower()

    def test_guest_pwyc_checkout_rejects_authenticated_user(
        self, member_client: Client, guest_event_with_tickets: Event, pwyc_tier: TicketTier
    ) -> None:
        """Test that authenticated users cannot use guest PWYC checkout."""
        # Arrange
        url = reverse(
            "api:guest_ticket_pwyc_checkout",
            kwargs={"event_id": guest_event_with_tickets.pk, "tier_id": pwyc_tier.pk},
        )
        payload = {
            "email": "auth@example.com",
            "first_name": "Auth",
            "last_name": "User",
            "tickets": [{"guest_name": "Auth User"}],
            "price_per_ticket": "15.00",
        }

        # Act
        response = member_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        data = response.json()
        assert "authenticated" in data["detail"].lower()
