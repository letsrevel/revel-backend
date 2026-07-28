"""Integration tests for the ReferralStripeController."""

import typing as t
from unittest.mock import Mock, patch

import pytest
import stripe
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import ReferralCode, RevelUser

pytestmark = pytest.mark.django_db


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture
def referrer(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="referrer@example.com",
        email="referrer@example.com",
        password="strong-password-123!",
    )


@pytest.fixture
def referral_code(referrer: RevelUser) -> ReferralCode:
    return ReferralCode.objects.create(user=referrer, code="REF123")


@pytest.fixture
def referrer_client(referrer: RevelUser) -> Client:
    refresh = RefreshToken.for_user(referrer)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def non_referrer(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="regular@example.com",
        email="regular@example.com",
        password="strong-password-123!",
    )


@pytest.fixture
def non_referrer_client(non_referrer: RevelUser) -> Client:
    refresh = RefreshToken.for_user(non_referrer)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# ---- POST /referral/stripe/connect ------------------------------------------


class TestReferralStripeConnect:
    url = reverse("api:referral_stripe_connect")

    def test_unauthenticated_returns_401(self, client: Client) -> None:
        response = client.post(self.url)
        assert response.status_code == 401

    def test_non_referrer_returns_403(self, non_referrer_client: Client) -> None:
        response = non_referrer_client.post(self.url)
        assert response.status_code == 403

    @patch("common.service.stripe_connect_service.stripe.AccountLink.create")
    @patch("common.service.stripe_connect_service.stripe.Account.create")
    def test_creates_express_account_and_returns_onboarding_url(
        self,
        mock_account_create: Mock,
        mock_link_create: Mock,
        referrer_client: Client,
        referral_code: ReferralCode,
        referrer: RevelUser,
    ) -> None:
        mock_account = Mock()
        mock_account.id = "acct_referrer_123"
        mock_account_create.return_value = mock_account

        mock_link = Mock()
        mock_link.url = "https://connect.stripe.com/setup/e/acct_referrer_123"
        mock_link_create.return_value = mock_link

        response = referrer_client.post(self.url)

        assert response.status_code == 200
        assert response.json()["onboarding_url"] == "https://connect.stripe.com/setup/e/acct_referrer_123"

        mock_account_create.assert_called_once_with(type="express", email="referrer@example.com")

        referrer.refresh_from_db()
        assert referrer.stripe_account_id == "acct_referrer_123"
        assert referrer.stripe_account_email == "referrer@example.com"

    @patch("common.service.stripe_connect_service.stripe.AccountLink.create")
    def test_reuses_existing_stripe_account_id(
        self,
        mock_link_create: Mock,
        referrer_client: Client,
        referral_code: ReferralCode,
        referrer: RevelUser,
    ) -> None:
        referrer.stripe_account_id = "acct_existing"
        referrer.save(update_fields=["stripe_account_id"])

        mock_link = Mock()
        mock_link.url = "https://connect.stripe.com/setup/e/acct_existing"
        mock_link_create.return_value = mock_link

        response = referrer_client.post(self.url)

        assert response.status_code == 200
        assert response.json()["onboarding_url"] == "https://connect.stripe.com/setup/e/acct_existing"

    def test_inactive_referral_code_returns_403(
        self,
        referrer_client: Client,
        referral_code: ReferralCode,
    ) -> None:
        referral_code.is_active = False
        referral_code.save(update_fields=["is_active"])

        response = referrer_client.post(self.url)
        assert response.status_code == 403


# ---- GET /referral/stripe/verify --------------------------------------------


class TestReferralStripeVerify:
    url = reverse("api:referral_stripe_verify")

    def test_unauthenticated_returns_401(self, client: Client) -> None:
        response = client.get(self.url)
        assert response.status_code == 401

    def test_non_referrer_returns_403(self, non_referrer_client: Client) -> None:
        response = non_referrer_client.get(self.url)
        assert response.status_code == 403

    @patch("common.service.stripe_connect_service.stripe.Account.retrieve")
    def test_syncs_account_status(
        self,
        mock_retrieve: Mock,
        referrer_client: Client,
        referral_code: ReferralCode,
        referrer: RevelUser,
    ) -> None:
        referrer.stripe_account_id = "acct_referrer_verify"
        referrer.save(update_fields=["stripe_account_id"])

        mock_account = Mock()
        mock_account.charges_enabled = True
        mock_account.details_submitted = True
        mock_retrieve.return_value = mock_account

        response = referrer_client.get(self.url)

        assert response.status_code == 200
        data = response.json()
        assert data["is_connected"] is True
        assert data["charges_enabled"] is True
        assert data["details_submitted"] is True

        referrer.refresh_from_db()
        assert referrer.stripe_charges_enabled is True
        assert referrer.stripe_details_submitted is True

    def test_returns_400_when_no_stripe_account(
        self,
        referrer_client: Client,
        referral_code: ReferralCode,
    ) -> None:
        response = referrer_client.get(self.url)
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "error",
        [
            stripe.error.PermissionError("The provided key 'sk_test_…' does not have access to account 'acct_stale'"),
            stripe.error.InvalidRequestError("No such account: 'acct_stale'", param="account"),
        ],
        ids=["permission_error", "invalid_request_error"],
    )
    @patch("common.service.stripe_connect_service.stripe.Account.retrieve")
    def test_inaccessible_account_reports_not_connected_instead_of_500(
        self,
        mock_retrieve: Mock,
        error: stripe.error.StripeError,
        referrer_client: Client,
        referral_code: ReferralCode,
        referrer: RevelUser,
    ) -> None:
        """A deleted/revoked/stale connected account must not 500 (see #694)."""
        referrer.stripe_account_id = "acct_stale"
        referrer.stripe_charges_enabled = True
        referrer.stripe_details_submitted = True
        referrer.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])

        mock_retrieve.side_effect = error

        response = referrer_client.get(self.url)

        assert response.status_code == 200
        assert response.json() == {
            "is_connected": False,
            "charges_enabled": False,
            "details_submitted": False,
        }

        referrer.refresh_from_db()
        assert referrer.stripe_charges_enabled is False
        assert referrer.stripe_details_submitted is False
        assert referrer.is_stripe_connected is False
