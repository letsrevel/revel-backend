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
from events.models import Event, Organization, TicketTier

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
            event_type=Event.Types.PUBLIC,
            max_attendees=100,
            start=next_week,
            end=next_week + timedelta(days=1),
            status=Event.Status.OPEN,
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

    @patch("events.service.stripe_service.create_checkout_session")
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
        mock_payment = Mock()
        mock_create_checkout.return_value = (checkout_url, mock_payment)

        # Act
        url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": paid_ticket_tier.pk})
        response = authenticated_client.post(url)

        # Assert
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["checkout_url"] == checkout_url

        mock_create_checkout.assert_called_once_with(
            public_event, paid_ticket_tier, organization_owner_user, price_override=None
        )

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
        response = client.post(url)

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
        response = authenticated_client.post(url)

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
        response = authenticated_client.post(url)

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
            event_type=Event.Types.PUBLIC,
            start=next_week,
            end=next_week + timedelta(days=1),
            status=Event.Status.OPEN,
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
        response = authenticated_client.post(url)

        # Assert
        assert response.status_code == 404  # get_object_or_404 filters by event

    @patch("events.service.stripe_service.create_checkout_session")
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
        response = authenticated_client.post(url)

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
            event_type=Event.Types.PRIVATE,
            start=datetime(2024, 12, 25, 12, tzinfo=timezone.get_current_timezone()),
            status=Event.Status.OPEN,
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
        response = client.post(url)

        # Assert
        assert response.status_code == 404  # Event not in user's queryset

    @patch("events.service.stripe_service.create_checkout_session")
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
        response = authenticated_client.post(url)

        # Assert
        assert response.status_code == 400
        response_data = response.json()
        assert "cannot be purchased" in response_data["detail"]

    @patch("events.service.stripe_service.create_checkout_session")
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
            event_type=Event.Types.PUBLIC,
            start=datetime(2024, 12, 25, 12, tzinfo=timezone.get_current_timezone()),
            status=Event.Status.OPEN,
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
        response = authenticated_client.post(url)

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
            event_type=Event.Types.PUBLIC,
            start=next_week,
            end=next_week + timedelta(days=1),
            status=Event.Status.OPEN,
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

    @patch("events.service.stripe_service.create_checkout_session")
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
        mock_payment = Mock()
        mock_create_checkout.return_value = (checkout_url, mock_payment)

        # Act - Make multiple rapid requests (this would normally be rate limited)
        responses = []
        for _ in range(3):
            url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": paid_ticket_tier.pk})
            response = authenticated_client.post(url)
            responses.append(response)

        # Assert - All should succeed due to increased rate limit in tests
        # (see conftest.py increase_rate_limit fixture)
        for response in responses:
            assert response.status_code == 200
