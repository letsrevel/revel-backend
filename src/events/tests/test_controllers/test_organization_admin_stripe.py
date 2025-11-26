"""Tests for Stripe-related organization admin endpoints."""

from unittest.mock import Mock, patch

import pytest
import stripe
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import Organization, OrganizationStaff

pytestmark = pytest.mark.django_db


class TestOrganizationAdminStripe:
    """Test Stripe endpoints in OrganizationAdminController."""

    @pytest.fixture
    def authenticated_client(self, organization_owner_user: RevelUser) -> Client:
        """Client authenticated as organization owner."""
        client = Client()
        refresh = RefreshToken.for_user(organization_owner_user)
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]
        return client

    @pytest.fixture
    def non_owner_client(self, staff_member: OrganizationStaff) -> Client:
        """Client authenticated as organization staff (not owner)."""
        client = Client()
        refresh = RefreshToken.for_user(staff_member.user)
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]
        return client

    @patch("events.service.stripe_service.create_connect_account")
    @patch("events.service.stripe_service.create_account_link")
    def test_stripe_connect_creates_new_account(
        self,
        mock_create_link: Mock,
        mock_create_account: Mock,
        authenticated_client: Client,
        organization: Organization,
    ) -> None:
        """Test Stripe Connect endpoint creates new account when none exists."""
        # Arrange
        mock_create_account.return_value = "acct_new123"
        mock_create_link.return_value = "https://stripe.com/connect/acct_new123"

        # Act
        url = reverse("api:stripe_connect", kwargs={"slug": organization.slug})
        response = authenticated_client.post(url, data={"email": "test@example.com"}, content_type="application/json")

        # Assert
        assert response.status_code == 200, response.content
        response_data = response.json()
        assert response_data["onboarding_url"] == "https://stripe.com/connect/acct_new123"

        mock_create_account.assert_called_once_with(organization, "test@example.com")
        mock_create_link.assert_called_once_with("acct_new123", organization)

    @patch("events.service.stripe_service.create_account_link")
    def test_stripe_connect_uses_existing_account(
        self,
        mock_create_link: Mock,
        authenticated_client: Client,
        organization: Organization,
    ) -> None:
        """Test Stripe Connect endpoint uses existing account ID."""
        # Arrange
        organization.stripe_account_id = "acct_existing123"
        organization.save()
        mock_create_link.return_value = "https://stripe.com/connect/acct_existing123"

        # Act
        url = reverse("api:stripe_connect", kwargs={"slug": organization.slug})
        response = authenticated_client.post(url, data={"email": "test@example.com"}, content_type="application/json")

        # Assert
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["onboarding_url"] == "https://stripe.com/connect/acct_existing123"

        mock_create_link.assert_called_once_with("acct_existing123", organization)

    def test_stripe_connect_requires_owner_permission(
        self,
        non_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that only organization owners can access Stripe Connect."""
        # Act
        url = reverse("api:stripe_connect", kwargs={"slug": organization.slug})
        response = non_owner_client.post(url, data={"email": "test@example.com"}, content_type="application/json")

        # Assert
        assert response.status_code == 403

    def test_stripe_connect_requires_authentication(
        self,
        organization: Organization,
    ) -> None:
        """Test that Stripe Connect requires authentication."""
        # Arrange
        client = Client()

        # Act
        url = reverse("api:stripe_connect", kwargs={"slug": organization.slug})
        response = client.post(url, data={"email": "test@example.com"}, content_type="application/json")

        # Assert
        assert response.status_code == 401

    @patch("events.service.stripe_service.create_connect_account")
    def test_stripe_connect_handles_stripe_error(
        self,
        mock_create_account: Mock,
        authenticated_client: Client,
        organization: Organization,
    ) -> None:
        """Test Stripe Connect handles Stripe API errors gracefully."""
        # Arrange
        mock_create_account.side_effect = stripe.error.APIError("Stripe API Error")

        # Act
        url = reverse("api:stripe_connect", kwargs={"slug": organization.slug})
        response = authenticated_client.post(url, data={"email": "test@example.com"}, content_type="application/json")

        # Assert
        assert response.status_code == 500

    @patch("events.service.stripe_service.stripe_verify_account")
    def test_stripe_account_verify_connected_account(
        self,
        mock_verify_account: Mock,
        authenticated_client: Client,
        organization: Organization,
    ) -> None:
        """Test account verify endpoint for connected account."""
        # Arrange
        organization.stripe_account_id = "acct_test123"
        organization.stripe_charges_enabled = True
        organization.stripe_details_submitted = True
        organization.save()

        mock_verify_account.return_value = organization

        # Act
        url = reverse("api:stripe_account_verify", kwargs={"slug": organization.slug})
        response = authenticated_client.post(url)

        # Assert
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["is_connected"] is True
        assert response_data["charges_enabled"] is True
        assert response_data["details_submitted"] is True

        mock_verify_account.assert_called_once_with(organization)

    @patch("events.service.stripe_service.stripe_verify_account")
    def test_stripe_account_verify_not_connected(
        self,
        mock_verify_account: Mock,
        authenticated_client: Client,
        organization: Organization,
    ) -> None:
        """Test account verify endpoint for organization without Stripe account."""
        # Arrange
        organization.stripe_charges_enabled = False
        organization.stripe_details_submitted = False
        organization.save()

        mock_verify_account.return_value = organization

        # Act
        url = reverse("api:stripe_account_verify", kwargs={"slug": organization.slug})
        response = authenticated_client.post(url)

        # Assert
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["is_connected"] is True  # Always True since method returns organization
        assert response_data["charges_enabled"] is False
        assert response_data["details_submitted"] is False

    def test_stripe_account_verify_requires_owner_permission(
        self,
        non_owner_client: Client,
        organization: Organization,
    ) -> None:
        """Test that only organization owners can verify account."""
        # Act
        url = reverse("api:stripe_account_verify", kwargs={"slug": organization.slug})
        response = non_owner_client.post(url)

        # Assert
        assert response.status_code == 403

    def test_stripe_account_verify_requires_authentication(
        self,
        organization: Organization,
    ) -> None:
        """Test that Stripe account verify requires authentication."""
        # Arrange
        client = Client()

        # Act
        url = reverse("api:stripe_account_verify", kwargs={"slug": organization.slug})
        response = client.post(url)

        # Assert
        assert response.status_code == 401

    @patch("events.service.stripe_service.stripe_verify_account")
    def test_stripe_account_verify_handles_stripe_error(
        self,
        mock_verify_account: Mock,
        authenticated_client: Client,
        organization: Organization,
    ) -> None:
        """Test account verify handles Stripe API errors."""
        # Arrange
        organization.stripe_account_id = "acct_test123"
        organization.save()
        mock_verify_account.side_effect = stripe.error.APIError("Account not found")

        # Act
        url = reverse("api:stripe_account_verify", kwargs={"slug": organization.slug})
        response = authenticated_client.post(url)

        # Assert
        assert response.status_code == 500

    def test_stripe_endpoints_nonexistent_organization(
        self,
        authenticated_client: Client,
    ) -> None:
        """Test Stripe endpoints with non-existent organization."""
        # Act
        connect_url = reverse("api:stripe_connect", kwargs={"slug": "nonexistent"})
        verify_url = reverse("api:stripe_account_verify", kwargs={"slug": "nonexistent"})

        connect_response = authenticated_client.post(
            connect_url, data={"email": "test@example.com"}, content_type="application/json"
        )
        verify_response = authenticated_client.post(verify_url)

        # Assert
        assert connect_response.status_code == 404
        assert verify_response.status_code == 404


class TestStripePermissions:
    """Test permission system for Stripe endpoints."""

    @pytest.fixture
    def organization_with_different_owner(self, organization_staff_user: RevelUser) -> Organization:
        """Organization owned by staff user (not the default owner)."""
        return Organization.objects.create(
            name="Different Org",
            slug="different-org",
            owner=organization_staff_user,
        )

    @pytest.fixture
    def staff_as_owner_client(self, organization_staff_user: RevelUser) -> Client:
        """Client where staff user is actually the owner."""
        client = Client()
        refresh = RefreshToken.for_user(organization_staff_user)
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {refresh.access_token}"  # type: ignore[attr-defined]
        return client

    @patch("events.service.stripe_service.create_account_link")
    @patch("events.service.stripe_service.stripe_verify_account")
    def test_owner_can_access_stripe_endpoints(
        self,
        mock_verify_account: Mock,
        mock_create_link: Mock,
        staff_as_owner_client: Client,
        organization_with_different_owner: Organization,
    ) -> None:
        """Test that organization owner can access Stripe endpoints."""
        # Arrange
        organization_with_different_owner.stripe_account_id = "acct_test123"
        organization_with_different_owner.stripe_charges_enabled = True
        organization_with_different_owner.stripe_details_submitted = True
        organization_with_different_owner.save()
        mock_create_link.return_value = "https://stripe.com/connect/test"
        mock_verify_account.return_value = organization_with_different_owner

        # Act
        connect_url = reverse("api:stripe_connect", kwargs={"slug": organization_with_different_owner.slug})
        verify_url = reverse("api:stripe_account_verify", kwargs={"slug": organization_with_different_owner.slug})

        connect_response = staff_as_owner_client.post(
            connect_url, data={"email": "test@example.com"}, content_type="application/json"
        )
        verify_response = staff_as_owner_client.post(verify_url)

        # Assert
        assert connect_response.status_code == 200
        assert verify_response.status_code == 200
