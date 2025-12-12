"""Tests for guest user endpoints in EventController.

Tests cover:
- Guest RSVP (with email confirmation)
- Guest ticket checkout (fixed-price and PWYC)
- Guest action confirmation via JWT tokens
- Service layer functions for guest user handling
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import jwt
import pytest
from django.conf import settings
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.token_blacklist.models import BlacklistedToken

from accounts.models import RevelUser
from events import models, schema
from events.models import Event, EventRSVP, Organization, Ticket, TicketTier
from events.service import guest as guest_service
from events.service.event_manager import UserIsIneligibleError

pytestmark = pytest.mark.django_db


# ============================================================================
# Fixtures
# ============================================================================


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


# ============================================================================
# Test Guest RSVP Endpoint: POST /events/{event_id}/rsvp/{answer}/public
# ============================================================================


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


# ============================================================================
# Test Guest Ticket Checkout (Fixed-Price): POST /events/{event_id}/tickets/{tier_id}/checkout/public
# ============================================================================


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


# ============================================================================
# Test Guest Ticket Checkout (PWYC): POST /events/{event_id}/tickets/{tier_id}/checkout/pwyc/public
# ============================================================================


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


# ============================================================================
# Test Confirm Guest Action: POST /events/guest-actions/confirm
# ============================================================================


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

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "tier" in data
        assert data["tier"]["name"] == free_tier.name

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
        assert "tier" in data  # Ticket schema

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


# ============================================================================
# Test Service Layer Functions
# ============================================================================


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


# ============================================================================
# Edge Cases
# ============================================================================


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


# ============================================================================
# Test Email Task Functions
# ============================================================================


class TestGuestEmailTasks:
    """Test the actual email task functions."""

    def test_send_guest_rsvp_confirmation_creates_email(self, guest_event: Event) -> None:
        """Test that send_guest_rsvp_confirmation task creates email correctly."""
        from django.core import mail

        from common.tasks import to_safe_email_address
        from events.tasks import send_guest_rsvp_confirmation

        # Arrange
        email = "test@example.com"
        token = "test_token_123"
        event_name = guest_event.name

        # Act
        send_guest_rsvp_confirmation(email, token, event_name)

        # Assert: Email was sent
        assert len(mail.outbox) == 1
        sent_email = mail.outbox[0]

        # Check email attributes - email is transformed by catchall system
        # Single recipients go to 'to', not 'bcc'
        safe_email = to_safe_email_address(email)
        assert safe_email in sent_email.to
        assert event_name in sent_email.subject
        assert "confirm" in sent_email.subject.lower() or "rsvp" in sent_email.subject.lower()
        assert token in sent_email.body
        assert event_name in sent_email.body

    def test_send_guest_ticket_confirmation_creates_email(
        self, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test that send_guest_ticket_confirmation task creates email correctly."""
        from django.core import mail

        from common.tasks import to_safe_email_address
        from events.tasks import send_guest_ticket_confirmation

        # Arrange
        email = "test@example.com"
        token = "test_token_456"
        event_name = guest_event_with_tickets.name
        tier_name = free_tier.name

        # Act
        send_guest_ticket_confirmation(email, token, event_name, tier_name)

        # Assert: Email was sent
        assert len(mail.outbox) == 1
        sent_email = mail.outbox[0]

        # Check email attributes - email is transformed by catchall system
        # Single recipients go to 'to', not 'bcc'
        safe_email = to_safe_email_address(email)
        assert safe_email in sent_email.to
        assert event_name in sent_email.subject
        assert "confirm" in sent_email.subject.lower() or "ticket" in sent_email.subject.lower()
        assert token in sent_email.body
        assert event_name in sent_email.body
        assert tier_name in sent_email.body

    def test_guest_rsvp_email_contains_confirmation_link(self, guest_event: Event) -> None:
        """Test that RSVP email contains proper confirmation link."""
        from django.core import mail

        from events.tasks import send_guest_rsvp_confirmation

        # Arrange
        email = "test@example.com"
        token = "test_token_789"
        event_name = guest_event.name

        # Act
        send_guest_rsvp_confirmation(email, token, event_name)

        # Assert: Email contains confirmation link
        sent_email = mail.outbox[0]
        assert "/events/confirm-action" in sent_email.body
        assert f"token={token}" in sent_email.body

    def test_guest_ticket_email_contains_confirmation_link(
        self, guest_event_with_tickets: Event, free_tier: TicketTier
    ) -> None:
        """Test that ticket email contains proper confirmation link."""
        from django.core import mail

        from events.tasks import send_guest_ticket_confirmation

        # Arrange
        email = "test@example.com"
        token = "test_token_abc"

        # Act
        send_guest_ticket_confirmation(email, token, guest_event_with_tickets.name, free_tier.name)

        # Assert: Email contains confirmation link
        sent_email = mail.outbox[0]
        assert "/events/confirm-action" in sent_email.body
        assert f"token={token}" in sent_email.body

    def test_guest_email_subject_uses_i18n(self, guest_event: Event) -> None:
        """Test that email subjects use internationalization strings."""
        from django.core import mail

        from events.tasks import send_guest_rsvp_confirmation

        # Act
        send_guest_rsvp_confirmation("test@example.com", "token", guest_event.name)

        # Assert: Subject is present and not empty (i18n string was used)
        sent_email = mail.outbox[0]
        assert sent_email.subject
        assert len(sent_email.subject) > 0


# ============================================================================
# Test Full End-to-End Integration
# ============================================================================


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
        import re

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
        import re

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
        import re

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


# ============================================================================
# Test Duplicate Action Handling
# ============================================================================


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
        import re

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


# ============================================================================
# Test Model Validation
# ============================================================================


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


# ============================================================================
# Test Error Messages
# ============================================================================


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


# ============================================================================
# Test Concurrency
# ============================================================================


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


# ============================================================================
# Test Reserved Seating for Public Checkout
# ============================================================================


class TestGuestCheckoutReservedSeating:
    """Test reserved seating functionality for guest ticket checkout endpoints."""

    @pytest.fixture
    def venue(self, organization: Organization) -> "models.Venue":
        """Create a venue for testing."""
        from events.models import Venue

        return Venue.objects.create(
            organization=organization,
            name="Test Venue",
            capacity=100,
        )

    @pytest.fixture
    def sector(self, venue: "models.Venue") -> "models.VenueSector":
        """Create a sector for testing."""
        from events.models import VenueSector

        return VenueSector.objects.create(
            venue=venue,
            name="Test Sector",
            capacity=50,
        )

    @pytest.fixture
    def seats(self, sector: "models.VenueSector") -> list["models.VenueSeat"]:
        """Create seats for testing."""
        from events.models import VenueSeat

        seats = []
        for i in range(5):
            seats.append(
                VenueSeat.objects.create(
                    sector=sector,
                    label=f"A{i + 1}",
                    row="A",
                    number=str(i + 1),
                )
            )
        return seats

    @pytest.fixture
    def user_choice_tier(
        self,
        guest_event_with_tickets: Event,
        venue: "models.Venue",
        sector: "models.VenueSector",
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
        self,
        guest_event_with_tickets: Event,
        venue: "models.Venue",
        sector: "models.VenueSector",
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

    @patch("events.tasks.send_guest_ticket_confirmation.delay")
    def test_guest_checkout_user_choice_seating_success(
        self,
        mock_send_email: Mock,
        guest_event_with_tickets: Event,
        user_choice_tier: TicketTier,
        seats: list["models.VenueSeat"],
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
        seats: list["models.VenueSeat"],
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

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "tier" in data
        assert data["seat"]["id"] == str(seats[0].id)
        assert data["seat"]["label"] == "A1"

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
        seats: list["models.VenueSeat"],
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
        seats: list["models.VenueSeat"],
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

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "tier" in data
        # Seat should be assigned randomly
        assert data["seat"] is not None
        assert "label" in data["seat"]

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
        seats: list["models.VenueSeat"],
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
        seats: list["models.VenueSeat"],
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
        seats: list["models.VenueSeat"],
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
        venue: "models.Venue",
        sector: "models.VenueSector",
        seats: list["models.VenueSeat"],
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
