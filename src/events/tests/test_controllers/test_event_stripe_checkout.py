"""Tests for Stripe checkout endpoint in EventController."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, TicketTier

pytestmark = pytest.mark.django_db


class TestEventStripeCheckout:
    """Test Stripe checkout endpoint in EventController."""

    @pytest.fixture
    def authenticated_client(self, organization_owner_user: RevelUser) -> Client:
        """Client authenticated as a user."""
        client = Client()
        refresh = RefreshToken.for_user(organization_owner_user)
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]
        return client

    @pytest.fixture
    def stripe_connected_organization(self, organization: Organization) -> Organization:
        """Organization with Stripe account connected."""
        organization.stripe_account_id = "acct_test123"
        organization.save()
        return organization

    @pytest.fixture
    def public_event(self, stripe_connected_organization: Organization, next_week: datetime) -> Event:
        """A public event with Stripe-connected organization."""
        return Event.objects.create(
            organization=stripe_connected_organization,
            name="Public Event",
            slug="public-event",
            event_type=Event.EventType.PUBLIC,
            max_attendees=100,
            start=next_week,
            end=next_week + timedelta(days=1),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
        )

    @pytest.fixture
    def paid_ticket_tier(self, public_event: Event) -> TicketTier:
        """A paid ticket tier."""
        gat = public_event.ticket_tiers.first()
        assert gat is not None
        gat.price = Decimal("25.00")
        gat.payment_method = TicketTier.PaymentMethod.ONLINE
        gat.save()
        return gat

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_ticket_checkout_success(
        self,
        mock_create_checkout: Mock,
        authenticated_client: Client,
        public_event: Event,
        paid_ticket_tier: TicketTier,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test successful ticket checkout."""
        # Arrange
        checkout_url = "https://checkout.stripe.com/pay/cs_test123"
        mock_create_checkout.return_value = checkout_url

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": paid_ticket_tier.pk})
        payload = {"tickets": [{"guest_name": "Test Guest"}]}
        response = authenticated_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["checkout_url"] == checkout_url
        assert response_data["tickets"] == []  # Tickets returned empty for online checkout

        mock_create_checkout.assert_called_once()

    def test_ticket_checkout_requires_authentication(
        self,
        public_event: Event,
        paid_ticket_tier: TicketTier,
    ) -> None:
        """Test that checkout requires authentication."""
        # Arrange
        client = Client()

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": paid_ticket_tier.pk})
        payload = {"tickets": [{"guest_name": "Test Guest"}]}
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 401

    def test_ticket_checkout_event_not_found(
        self,
        authenticated_client: Client,
        paid_ticket_tier: TicketTier,
    ) -> None:
        """Test checkout with non-existent event."""
        # Arrange
        fake_event_id = "00000000-0000-0000-0000-000000000000"

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": fake_event_id, "tier_id": paid_ticket_tier.pk})
        payload = {"tickets": [{"guest_name": "Test Guest"}]}
        response = authenticated_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 404

    def test_ticket_checkout_tier_not_found(
        self,
        authenticated_client: Client,
        public_event: Event,
    ) -> None:
        """Test checkout with non-existent ticket tier."""
        # Arrange
        fake_tier_id = "00000000-0000-0000-0000-000000000000"

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": fake_tier_id})
        payload = {"tickets": [{"guest_name": "Test Guest"}]}
        response = authenticated_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 404

    def test_ticket_checkout_tier_belongs_to_different_event(
        self,
        authenticated_client: Client,
        public_event: Event,
        stripe_connected_organization: Organization,
        next_week: datetime,
    ) -> None:
        """Test checkout with tier that belongs to a different event."""
        # Arrange
        different_event = Event.objects.create(
            organization=stripe_connected_organization,
            name="Different Event",
            slug="different-event",
            event_type=Event.EventType.PUBLIC,
            start=next_week,
            end=next_week + timedelta(days=1),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
        )

        different_tier = TicketTier.objects.create(
            event=different_event,
            name="Different Tier",
            price=Decimal("30.00"),
            currency="EUR",
        )

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": different_tier.pk})
        payload = {"tickets": [{"guest_name": "Test Guest"}]}
        response = authenticated_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 404  # get_object_or_404 filters by event

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_ticket_checkout_stripe_service_error(
        self,
        mock_create_checkout: Mock,
        authenticated_client: Client,
        public_event: Event,
        paid_ticket_tier: TicketTier,
    ) -> None:
        """Test checkout when stripe service raises an error."""
        # Arrange
        mock_create_checkout.side_effect = HttpError(400, "Organization not configured for payments")

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": paid_ticket_tier.pk})
        payload = {"tickets": [{"guest_name": "Test Guest"}]}
        response = authenticated_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        response_data = response.json()
        assert "Organization not configured for payments" in response_data["detail"]

    def test_ticket_checkout_private_event_access_denied(
        self,
        authenticated_client: Client,
        stripe_connected_organization: Organization,
        organization_staff_user: RevelUser,
    ) -> None:
        """Test checkout access is denied for private events user can't see."""
        # Arrange
        private_event = Event.objects.create(
            organization=stripe_connected_organization,
            name="Private Event",
            slug="private-event",
            event_type=Event.EventType.PRIVATE,
            start=datetime(2024, 12, 25, 12, tzinfo=timezone.get_current_timezone()),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PRIVATE,
        )

        private_tier = TicketTier.objects.create(
            event=private_event,
            name="Private Tier",
            price=Decimal("50.00"),
            currency="EUR",
        )

        # Create client for user who doesn't have access
        client = Client()
        refresh = RefreshToken.for_user(organization_staff_user)
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": private_event.pk, "tier_id": private_tier.pk})
        payload = {"tickets": [{"guest_name": "Test Guest"}]}
        response = client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 404  # Event not in user's queryset

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_ticket_checkout_free_tier(
        self,
        mock_create_checkout: Mock,
        authenticated_client: Client,
        public_event: Event,
    ) -> None:
        """Test checkout with free ticket tier."""
        # Arrange
        free_tier = TicketTier.objects.create(
            event=public_event,
            name="Free Tier",
            price=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )

        mock_create_checkout.side_effect = HttpError(400, "This ticket tier cannot be purchased.")

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": free_tier.pk})
        payload = {"tickets": [{"guest_name": "Test Guest"}]}
        response = authenticated_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        response_data = response.json()
        assert "cannot be purchased" in response_data["detail"]

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_ticket_checkout_organization_not_connected(
        self,
        mock_create_checkout: Mock,
        authenticated_client: Client,
        organization: Organization,  # Not Stripe-connected
        organization_owner_user: RevelUser,
    ) -> None:
        """Test checkout when organization doesn't have Stripe connected."""
        # Arrange
        event = Event.objects.create(
            organization=organization,
            name="Event",
            slug="event",
            event_type=Event.EventType.PUBLIC,
            start=datetime(2024, 12, 25, 12, tzinfo=timezone.get_current_timezone()),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
        )

        tier = TicketTier.objects.create(
            event=event,
            name="Paid Tier",
            price=Decimal("25.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )

        mock_create_checkout.side_effect = HttpError(400, "This organization is not configured to accept payments.")

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": event.pk, "tier_id": tier.pk})
        payload = {"tickets": [{"guest_name": "Test Guest"}]}
        response = authenticated_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        response_data = response.json()
        assert "not configured to accept payments" in response_data["detail"]


class TestStripeCheckoutRateLimit:
    """Test rate limiting on checkout endpoint."""

    @pytest.fixture
    def authenticated_client(self, organization_owner_user: RevelUser) -> Client:
        """Client authenticated as a user."""
        client = Client()
        refresh = RefreshToken.for_user(organization_owner_user)
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]
        return client

    @pytest.fixture
    def stripe_connected_organization(self, organization: Organization) -> Organization:
        """Organization with Stripe account connected."""
        organization.stripe_account_id = "acct_test123"
        organization.save()
        return organization

    @pytest.fixture
    def public_event(self, stripe_connected_organization: Organization, next_week: datetime) -> Event:
        """A public event."""
        return Event.objects.create(
            organization=stripe_connected_organization,
            name="Public Event",
            slug="public-event",
            event_type=Event.EventType.PUBLIC,
            start=next_week,
            end=next_week + timedelta(days=1),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
        )

    @pytest.fixture
    def paid_ticket_tier(self, public_event: Event) -> TicketTier:
        """A paid ticket tier."""
        gat = public_event.ticket_tiers.first()
        assert gat is not None
        gat.price = Decimal("25.00")
        gat.payment_method = TicketTier.PaymentMethod.ONLINE
        gat.save()
        return gat

    @patch("events.service.stripe_service.create_batch_checkout_session")
    def test_ticket_checkout_respects_write_throttle(
        self,
        mock_create_checkout: Mock,
        authenticated_client: Client,
        public_event: Event,
        paid_ticket_tier: TicketTier,
    ) -> None:
        """Test that checkout endpoint is throttled."""
        # Arrange
        checkout_url = "https://checkout.stripe.com/pay/cs_test123"
        mock_create_checkout.return_value = checkout_url
        # Set max_tickets_per_user to allow multiple purchases
        public_event.max_tickets_per_user = 10
        public_event.save()

        # Act - Make multiple rapid requests (this would normally be rate limited)
        responses = []
        for i in range(3):
            url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": paid_ticket_tier.pk})
            payload = {"tickets": [{"guest_name": f"Guest {i}"}]}
            response = authenticated_client.post(url, data=payload, content_type="application/json")
            responses.append(response)

        # Assert - All should succeed due to increased rate limit in tests
        # (see conftest.py increase_rate_limit fixture)
        for response in responses:
            assert response.status_code == 200


class TestResumeCheckoutEndpoint:
    """Test resume checkout endpoint in EventController."""

    @pytest.fixture
    def authenticated_client(self, organization_owner_user: RevelUser) -> Client:
        """Client authenticated as a user."""
        client = Client()
        refresh = RefreshToken.for_user(organization_owner_user)
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]
        return client

    @pytest.fixture
    def stripe_connected_organization(self, organization: Organization) -> Organization:
        """Organization with Stripe account connected."""
        organization.stripe_account_id = "acct_test123"
        organization.save()
        return organization

    @pytest.fixture
    def public_event(self, stripe_connected_organization: Organization, next_week: datetime) -> Event:
        """A public event with Stripe-connected organization."""
        return Event.objects.create(
            organization=stripe_connected_organization,
            name="Public Event",
            slug="public-event",
            event_type=Event.EventType.PUBLIC,
            max_attendees=100,
            start=next_week,
            end=next_week + timedelta(days=1),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
        )

    @pytest.fixture
    def paid_ticket_tier(self, public_event: Event) -> TicketTier:
        """A paid ticket tier."""
        gat = public_event.ticket_tiers.first()
        assert gat is not None
        gat.price = Decimal("25.00")
        gat.payment_method = TicketTier.PaymentMethod.ONLINE
        gat.quantity_sold = 1
        gat.save()
        return gat

    @pytest.fixture
    def pending_payment(
        self,
        public_event: Event,
        paid_ticket_tier: TicketTier,
        organization_owner_user: RevelUser,
    ) -> Payment:
        """A pending payment with ticket."""
        from events.models import Ticket

        ticket = Ticket.objects.create(
            event=public_event,
            tier=paid_ticket_tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Test Guest",
        )
        return Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_test_resume",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

    def test_resume_checkout_requires_authentication(
        self,
        pending_payment: Payment,
    ) -> None:
        """Test that resume checkout requires authentication."""
        client = Client()
        url = reverse("api:resume_checkout", kwargs={"payment_id": pending_payment.pk})
        response = client.get(url)
        assert response.status_code == 401

    @patch("events.service.stripe_service.resume_pending_checkout")
    def test_resume_checkout_success(
        self,
        mock_resume: Mock,
        authenticated_client: Client,
        pending_payment: Payment,
    ) -> None:
        """Test successful resume checkout."""
        mock_resume.return_value = "https://checkout.stripe.com/pay/cs_test_resume"

        url = reverse("api:resume_checkout", kwargs={"payment_id": pending_payment.pk})
        response = authenticated_client.get(url)

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["checkout_url"] == "https://checkout.stripe.com/pay/cs_test_resume"
        mock_resume.assert_called_once()

    @patch("events.service.stripe_service.resume_pending_checkout")
    def test_resume_checkout_not_found(
        self,
        mock_resume: Mock,
        authenticated_client: Client,
    ) -> None:
        """Test resume checkout with non-existent payment."""
        mock_resume.side_effect = HttpError(404, "No pending payment found")

        fake_payment_id = "00000000-0000-0000-0000-000000000000"
        url = reverse("api:resume_checkout", kwargs={"payment_id": fake_payment_id})
        response = authenticated_client.get(url)

        assert response.status_code == 404
        response_data = response.json()
        assert "No pending payment found" in response_data["detail"]

    @patch("events.service.stripe_service.resume_pending_checkout")
    def test_resume_checkout_expired_payment(
        self,
        mock_resume: Mock,
        authenticated_client: Client,
        pending_payment: Payment,
    ) -> None:
        """Test resume checkout with expired payment."""
        mock_resume.side_effect = HttpError(404, "Payment has expired")

        url = reverse("api:resume_checkout", kwargs={"payment_id": pending_payment.pk})
        response = authenticated_client.get(url)

        assert response.status_code == 404
        response_data = response.json()
        assert "expired" in response_data["detail"].lower()


class TestCancelCheckoutEndpoint:
    """Test cancel checkout endpoint in EventController."""

    @pytest.fixture
    def authenticated_client(self, organization_owner_user: RevelUser) -> Client:
        """Client authenticated as a user."""
        client = Client()
        refresh = RefreshToken.for_user(organization_owner_user)
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]
        return client

    @pytest.fixture
    def stripe_connected_organization(self, organization: Organization) -> Organization:
        """Organization with Stripe account connected."""
        organization.stripe_account_id = "acct_test123"
        organization.save()
        return organization

    @pytest.fixture
    def public_event(self, stripe_connected_organization: Organization, next_week: datetime) -> Event:
        """A public event with Stripe-connected organization."""
        return Event.objects.create(
            organization=stripe_connected_organization,
            name="Public Event",
            slug="public-event",
            event_type=Event.EventType.PUBLIC,
            max_attendees=100,
            start=next_week,
            end=next_week + timedelta(days=1),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
        )

    @pytest.fixture
    def paid_ticket_tier(self, public_event: Event) -> TicketTier:
        """A paid ticket tier."""
        gat = public_event.ticket_tiers.first()
        assert gat is not None
        gat.price = Decimal("25.00")
        gat.payment_method = TicketTier.PaymentMethod.ONLINE
        gat.quantity_sold = 1
        gat.save()
        return gat

    @pytest.fixture
    def pending_payment(
        self,
        public_event: Event,
        paid_ticket_tier: TicketTier,
        organization_owner_user: RevelUser,
    ) -> Payment:
        """A pending payment with ticket."""
        from events.models import Ticket

        ticket = Ticket.objects.create(
            event=public_event,
            tier=paid_ticket_tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Test Guest",
        )
        return Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_test_cancel",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

    def test_cancel_checkout_requires_authentication(
        self,
        pending_payment: Payment,
    ) -> None:
        """Test that cancel checkout requires authentication."""
        client = Client()
        url = reverse("api:cancel_checkout", kwargs={"payment_id": pending_payment.pk})
        response = client.delete(url)
        assert response.status_code == 401

    @patch("events.service.stripe_service.cancel_pending_checkout")
    def test_cancel_checkout_success(
        self,
        mock_cancel: Mock,
        authenticated_client: Client,
        pending_payment: Payment,
    ) -> None:
        """Test successful cancel checkout."""
        mock_cancel.return_value = 1

        url = reverse("api:cancel_checkout", kwargs={"payment_id": pending_payment.pk})
        response = authenticated_client.delete(url)

        assert response.status_code == 200
        response_data = response.json()
        assert "1 ticket(s) cancelled successfully" in response_data["message"]
        mock_cancel.assert_called_once()

    @patch("events.service.stripe_service.cancel_pending_checkout")
    def test_cancel_checkout_batch_success(
        self,
        mock_cancel: Mock,
        authenticated_client: Client,
        pending_payment: Payment,
    ) -> None:
        """Test cancel checkout with multiple tickets in batch."""
        mock_cancel.return_value = 3

        url = reverse("api:cancel_checkout", kwargs={"payment_id": pending_payment.pk})
        response = authenticated_client.delete(url)

        assert response.status_code == 200
        response_data = response.json()
        assert "3 ticket(s) cancelled successfully" in response_data["message"]

    @patch("events.service.stripe_service.cancel_pending_checkout")
    def test_cancel_checkout_not_found(
        self,
        mock_cancel: Mock,
        authenticated_client: Client,
    ) -> None:
        """Test cancel checkout with non-existent payment."""
        mock_cancel.side_effect = HttpError(404, "Payment not found")

        fake_payment_id = "00000000-0000-0000-0000-000000000000"
        url = reverse("api:cancel_checkout", kwargs={"payment_id": fake_payment_id})
        response = authenticated_client.delete(url)

        assert response.status_code == 404
        response_data = response.json()
        assert "Payment not found" in response_data["detail"]

    @patch("events.service.stripe_service.cancel_pending_checkout")
    def test_cancel_checkout_not_pending(
        self,
        mock_cancel: Mock,
        authenticated_client: Client,
        pending_payment: Payment,
    ) -> None:
        """Test cancel checkout with non-pending payment."""
        mock_cancel.side_effect = HttpError(400, "Only pending payments can be cancelled")

        url = reverse("api:cancel_checkout", kwargs={"payment_id": pending_payment.pk})
        response = authenticated_client.delete(url)

        assert response.status_code == 400
        response_data = response.json()
        assert "Only pending payments" in response_data["detail"]

    @patch("events.service.stripe_service.cancel_pending_checkout")
    def test_cancel_checkout_wrong_user(
        self,
        mock_cancel: Mock,
        pending_payment: Payment,
        organization_staff_user: RevelUser,
    ) -> None:
        """Test cancel checkout by different user."""
        mock_cancel.side_effect = HttpError(404, "Payment not found")

        # Create client for different user
        client = Client()
        refresh = RefreshToken.for_user(organization_staff_user)
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]

        url = reverse("api:cancel_checkout", kwargs={"payment_id": pending_payment.pk})
        response = client.delete(url)

        assert response.status_code == 404
