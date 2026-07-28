"""Tests for Stripe Connect account management functions."""

from unittest.mock import Mock, patch

import pytest
import stripe

from common.service.stripe_connect_service import sync_account_status
from events.models import Organization
from events.service import stripe_service

pytestmark = pytest.mark.django_db


class TestCreateConnectAccount:
    """Test create_connect_account function."""

    @patch("stripe.Account.create")
    def test_creates_stripe_account_and_saves_id(
        self,
        mock_stripe_create: Mock,
        organization: Organization,
    ) -> None:
        """Test that a Stripe Connect account is created and ID is saved."""
        # Arrange
        mock_account = Mock()
        mock_account.id = "acct_test123"
        mock_stripe_create.return_value = mock_account

        # Act
        result = stripe_service.create_connect_account(organization, organization.owner.email)

        # Assert
        mock_stripe_create.assert_called_once_with(type="standard", email=organization.owner.email)
        organization.refresh_from_db()
        assert organization.stripe_account_id == "acct_test123"
        assert result == "acct_test123"

    @patch("stripe.Account.create")
    def test_handles_stripe_api_error(
        self,
        mock_stripe_create: Mock,
        organization: Organization,
    ) -> None:
        """Test that Stripe API errors are propagated."""
        # Arrange
        mock_stripe_create.side_effect = stripe.error.APIError("API Error")

        # Act & Assert
        with pytest.raises(stripe.error.APIError):
            stripe_service.create_connect_account(organization, organization.owner.email)

        # Verify organization wasn't modified
        organization.refresh_from_db()
        assert organization.stripe_account_id is None


class TestCreateAccountLink:
    """Test create_account_link function."""

    @patch("stripe.AccountLink.create")
    def test_creates_onboarding_link(
        self,
        mock_stripe_create: Mock,
        organization: Organization,
    ) -> None:
        """Test that onboarding link is created with correct URLs."""
        from django.conf import settings

        # Arrange
        account_id = "acct_test123"
        mock_link = Mock()
        mock_link.url = "https://stripe.com/onboard/test"
        mock_stripe_create.return_value = mock_link

        # Act
        result = stripe_service.create_account_link(account_id, organization)

        # Assert
        expected_refresh_url = (
            f"{settings.FRONTEND_BASE_URL}/org/{organization.slug}/admin/settings?stripe_refresh=true"
        )
        expected_return_url = f"{settings.FRONTEND_BASE_URL}/org/{organization.slug}/admin/settings?stripe_success=true"

        mock_stripe_create.assert_called_once_with(
            account=account_id,
            refresh_url=expected_refresh_url,
            return_url=expected_return_url,
            type="account_onboarding",
        )
        assert result == "https://stripe.com/onboard/test"

    @patch("stripe.AccountLink.create")
    def test_handles_stripe_api_error(self, mock_stripe_create: Mock, organization: Organization) -> None:
        """Test that Stripe API errors are propagated from create_account_link."""
        # Arrange
        mock_stripe_create.side_effect = stripe.error.APIError("API Error")
        account_id = "acct_test123"

        # Act & Assert
        with pytest.raises(stripe.error.APIError):
            stripe_service.create_account_link(account_id, organization)


class TestGetAccountDetails:
    """Test get_account_details function."""

    @patch("stripe.Account.retrieve")
    def test_retrieves_account_details(self, mock_stripe_retrieve: Mock) -> None:
        """Test that account details are retrieved from Stripe."""
        # Arrange
        account_id = "acct_test123"
        mock_account = Mock(spec=stripe.Account)
        mock_stripe_retrieve.return_value = mock_account

        # Act
        result = stripe_service.get_account_details(account_id)

        # Assert
        mock_stripe_retrieve.assert_called_once_with(account_id)
        assert result == mock_account

    @patch("stripe.Account.retrieve")
    def test_handles_stripe_api_error(self, mock_stripe_retrieve: Mock) -> None:
        """Test that Stripe API errors are propagated from get_account_details."""
        # Arrange
        mock_stripe_retrieve.side_effect = stripe.error.APIError("API Error")
        account_id = "acct_test123"

        # Act & Assert
        with pytest.raises(stripe.error.APIError):
            stripe_service.get_account_details(account_id)


class TestSyncAccountStatusInaccessibleAccount:
    """``sync_account_status`` must not blow up when the stored account is unreachable."""

    @pytest.fixture
    def connected_organization(self, organization: Organization) -> Organization:
        organization.stripe_account_id = "acct_gone"
        organization.stripe_charges_enabled = True
        organization.stripe_details_submitted = True
        organization.save(
            update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted", "updated_at"]
        )
        return organization

    @pytest.mark.parametrize(
        "error",
        [
            stripe.error.PermissionError("The provided key does not have access to account 'acct_gone'"),
            stripe.error.InvalidRequestError("No such account: 'acct_gone'", param="account"),
        ],
        ids=["permission_error", "invalid_request_error"],
    )
    @patch("stripe.Account.retrieve")
    def test_flags_account_as_not_connected(
        self,
        mock_stripe_retrieve: Mock,
        error: stripe.error.StripeError,
        connected_organization: Organization,
    ) -> None:
        """An inaccessible account returns ``None`` and resets the connection flags."""
        # Arrange
        mock_stripe_retrieve.side_effect = error

        # Act
        result = sync_account_status(connected_organization)

        # Assert
        assert result is None
        connected_organization.refresh_from_db()
        assert connected_organization.stripe_charges_enabled is False
        assert connected_organization.stripe_details_submitted is False
        assert connected_organization.is_stripe_connected is False
        # The ID is kept: clearing it would be irreversible if the platform key is misconfigured.
        assert connected_organization.stripe_account_id == "acct_gone"

    @patch("stripe.Account.retrieve")
    def test_verify_account_returns_organization_without_billing_autofill(
        self,
        mock_stripe_retrieve: Mock,
        connected_organization: Organization,
    ) -> None:
        """``stripe_verify_account`` degrades gracefully instead of raising."""
        # Arrange
        mock_stripe_retrieve.side_effect = stripe.error.PermissionError("no access")

        # Act
        result = stripe_service.stripe_verify_account(connected_organization)

        # Assert
        assert result is connected_organization
        assert result.stripe_charges_enabled is False
        assert result.stripe_details_submitted is False
        assert result.billing_address == ""

    @patch("stripe.Account.retrieve")
    def test_other_stripe_errors_still_propagate(
        self,
        mock_stripe_retrieve: Mock,
        connected_organization: Organization,
    ) -> None:
        """Unexpected Stripe failures must not be swallowed."""
        # Arrange
        mock_stripe_retrieve.side_effect = stripe.error.APIError("boom")

        # Act & Assert
        with pytest.raises(stripe.error.APIError):
            sync_account_status(connected_organization)
