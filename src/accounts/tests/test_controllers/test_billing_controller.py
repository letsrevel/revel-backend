"""Integration tests for the UserBillingController."""

from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser, UserBillingProfile
from common.service.vies_service import VIESValidationResult

pytestmark = pytest.mark.django_db


@pytest.fixture
def billing_profile(user: RevelUser) -> UserBillingProfile:
    return UserBillingProfile.objects.create(
        user=user,
        billing_name="Test User",
        billing_address="Via Roma 1",
        billing_country="IT",
        billing_email="billing@example.com",
    )


# ===========================================================================
# GET /me/billing
# ===========================================================================


class TestGetBillingProfile:
    def test_returns_profile(self, auth_client: Client, billing_profile: UserBillingProfile) -> None:
        """Authenticated user can retrieve their billing profile."""
        url = reverse("api:get_billing_profile")
        response = auth_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["billing_name"] == "Test User"
        assert data["billing_address"] == "Via Roma 1"
        assert data["billing_country"] == "IT"
        assert data["billing_email"] == "billing@example.com"

    def test_returns_404_when_no_profile(self, auth_client: Client) -> None:
        """Returns 404 when user has no billing profile."""
        url = reverse("api:get_billing_profile")
        response = auth_client.get(url)

        assert response.status_code == 404

    def test_unauthenticated_returns_401(self, client: Client) -> None:
        """Unauthenticated request returns 401."""
        url = reverse("api:get_billing_profile")
        response = client.get(url)

        assert response.status_code == 401


# ===========================================================================
# POST /me/billing
# ===========================================================================


class TestCreateBillingProfile:
    def test_creates_profile(self, auth_client: Client) -> None:
        """Authenticated user can create a billing profile."""
        url = reverse("api:create_billing_profile")
        response = auth_client.post(
            url,
            data=orjson.dumps(
                {
                    "billing_name": "John Doe",
                    "billing_address": "123 Main St",
                    "billing_country": "DE",
                    "billing_email": "john@example.com",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 201
        data = response.json()
        assert data["billing_name"] == "John Doe"
        assert data["billing_country"] == "DE"

    def test_creates_profile_minimal(self, auth_client: Client) -> None:
        """Only billing_name is required."""
        url = reverse("api:create_billing_profile")
        response = auth_client.post(
            url,
            data=orjson.dumps({"billing_name": "Jane Doe"}),
            content_type="application/json",
        )

        assert response.status_code == 201
        data = response.json()
        assert data["billing_name"] == "Jane Doe"
        assert data["billing_address"] == ""
        assert data["billing_country"] == ""
        assert data["billing_email"] == ""

    def test_rejects_duplicate_profile(self, auth_client: Client, billing_profile: UserBillingProfile) -> None:
        """Cannot create a second billing profile."""
        url = reverse("api:create_billing_profile")
        response = auth_client.post(
            url,
            data=orjson.dumps({"billing_name": "Duplicate"}),
            content_type="application/json",
        )

        assert response.status_code == 409

    def test_rejects_empty_billing_name(self, auth_client: Client) -> None:
        """billing_name cannot be empty."""
        url = reverse("api:create_billing_profile")
        response = auth_client.post(
            url,
            data=orjson.dumps({"billing_name": ""}),
            content_type="application/json",
        )

        assert response.status_code == 422

    def test_unauthenticated_returns_401(self, client: Client) -> None:
        """Unauthenticated request returns 401."""
        url = reverse("api:create_billing_profile")
        response = client.post(
            url,
            data=orjson.dumps({"billing_name": "Test"}),
            content_type="application/json",
        )

        assert response.status_code == 401


# ===========================================================================
# PATCH /me/billing
# ===========================================================================


class TestUpdateBillingProfile:
    def test_updates_single_field(self, auth_client: Client, billing_profile: UserBillingProfile) -> None:
        """Can update a single field without affecting others."""
        url = reverse("api:update_billing_profile")
        response = auth_client.patch(
            url,
            data=orjson.dumps({"billing_name": "Updated Name"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["billing_name"] == "Updated Name"
        assert data["billing_address"] == "Via Roma 1"  # unchanged

    def test_updates_multiple_fields(self, auth_client: Client, billing_profile: UserBillingProfile) -> None:
        """Can update multiple fields at once."""
        url = reverse("api:update_billing_profile")
        response = auth_client.patch(
            url,
            data=orjson.dumps(
                {
                    "billing_name": "New Name",
                    "billing_country": "DE",
                    "billing_email": "new@example.com",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["billing_name"] == "New Name"
        assert data["billing_country"] == "DE"
        assert data["billing_email"] == "new@example.com"

    def test_empty_body_returns_unchanged(self, auth_client: Client, billing_profile: UserBillingProfile) -> None:
        """Empty update body returns the profile unchanged."""
        url = reverse("api:update_billing_profile")
        response = auth_client.patch(
            url,
            data=orjson.dumps({}),
            content_type="application/json",
        )

        assert response.status_code == 200
        assert response.json()["billing_name"] == "Test User"

    def test_null_values_are_ignored(self, auth_client: Client, billing_profile: UserBillingProfile) -> None:
        """Sending null for a field does not clear it."""
        url = reverse("api:update_billing_profile")
        response = auth_client.patch(
            url,
            data=orjson.dumps({"billing_name": None, "billing_address": "New Address"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["billing_name"] == "Test User"  # unchanged, null ignored
        assert data["billing_address"] == "New Address"

    def test_returns_404_when_no_profile(self, auth_client: Client) -> None:
        """Returns 404 when user has no billing profile."""
        url = reverse("api:update_billing_profile")
        response = auth_client.patch(
            url,
            data=orjson.dumps({"billing_name": "No Profile"}),
            content_type="application/json",
        )

        assert response.status_code == 404


# ===========================================================================
# PUT /me/billing/vat-id
# ===========================================================================


class TestSetVATId:
    @patch("accounts.controllers.billing.validate_and_update_billing_profile")
    def test_sets_vat_id_with_valid_vies_result(
        self, mock_validate: MagicMock, auth_client: Client, billing_profile: UserBillingProfile
    ) -> None:
        """Setting a valid VAT ID triggers VIES validation and updates the profile."""
        mock_validate.return_value = VIESValidationResult(
            valid=True, name="ACME SRL", address="VIA ROMA 1", request_identifier="REQ123"
        )

        url = reverse("api:set_billing_vat_id")
        response = auth_client.put(
            url,
            data=orjson.dumps({"vat_id": "IT12345678901"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["vat_id"] == "IT12345678901"
        assert data["vat_country_code"] == "IT"
        mock_validate.assert_called_once()

    @patch("accounts.controllers.billing.validate_and_update_billing_profile")
    def test_invalid_vat_id_clears_saved_data(
        self, mock_validate: MagicMock, auth_client: Client, billing_profile: UserBillingProfile
    ) -> None:
        """Invalid VIES result rolls back the saved VAT ID."""
        mock_validate.return_value = VIESValidationResult(valid=False, name="", address="", request_identifier="REQ456")

        url = reverse("api:set_billing_vat_id")
        response = auth_client.put(
            url,
            data=orjson.dumps({"vat_id": "IT00000000000"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        billing_profile.refresh_from_db()
        assert billing_profile.vat_id == ""
        assert billing_profile.vat_country_code == ""
        assert billing_profile.vat_id_validated is False
        assert billing_profile.vat_id_validated_at is None
        assert billing_profile.vies_request_identifier == ""

    @patch("accounts.controllers.billing.validate_and_update_billing_profile")
    def test_invalid_vat_id_returns_400(
        self, mock_validate: MagicMock, auth_client: Client, billing_profile: UserBillingProfile
    ) -> None:
        """VIES returning invalid results in 400."""
        mock_validate.return_value = VIESValidationResult(valid=False, name="", address="", request_identifier="REQ456")

        url = reverse("api:set_billing_vat_id")
        response = auth_client.put(
            url,
            data=orjson.dumps({"vat_id": "IT00000000000"}),
            content_type="application/json",
        )

        assert response.status_code == 400

    @patch("accounts.controllers.billing.validate_and_update_billing_profile")
    def test_vies_unavailable_returns_503(
        self, mock_validate: MagicMock, auth_client: Client, billing_profile: UserBillingProfile
    ) -> None:
        """VIES being unavailable returns 503."""
        from common.service.vies_service import VIESUnavailableError

        mock_validate.side_effect = VIESUnavailableError("Connection refused")

        url = reverse("api:set_billing_vat_id")
        response = auth_client.put(
            url,
            data=orjson.dumps({"vat_id": "IT12345678901"}),
            content_type="application/json",
        )

        assert response.status_code == 503

    def test_rejects_invalid_format(self, auth_client: Client, billing_profile: UserBillingProfile) -> None:
        """VAT ID with invalid format is rejected at schema level."""
        url = reverse("api:set_billing_vat_id")
        response = auth_client.put(
            url,
            data=orjson.dumps({"vat_id": "INVALID"}),
            content_type="application/json",
        )

        assert response.status_code == 422

    def test_rejects_non_eu_country(self, auth_client: Client, billing_profile: UserBillingProfile) -> None:
        """VAT ID with non-EU country prefix is rejected."""
        url = reverse("api:set_billing_vat_id")
        response = auth_client.put(
            url,
            data=orjson.dumps({"vat_id": "US12345678901"}),
            content_type="application/json",
        )

        assert response.status_code == 422

    def test_returns_404_when_no_profile(self, auth_client: Client) -> None:
        """Returns 404 when user has no billing profile."""
        url = reverse("api:set_billing_vat_id")
        response = auth_client.put(
            url,
            data=orjson.dumps({"vat_id": "IT12345678901"}),
            content_type="application/json",
        )

        assert response.status_code == 404


# ===========================================================================
# DELETE /me/billing/vat-id
# ===========================================================================


class TestDeleteVATId:
    def test_clears_vat_id(self, auth_client: Client, billing_profile: UserBillingProfile) -> None:
        """Deleting VAT ID clears all VAT-related fields."""
        billing_profile.vat_id = "IT12345678901"
        billing_profile.vat_country_code = "IT"
        billing_profile.vat_id_validated = True
        billing_profile.save(update_fields=["vat_id", "vat_country_code", "vat_id_validated"])

        url = reverse("api:delete_billing_vat_id")
        response = auth_client.delete(url)

        assert response.status_code == 204

        billing_profile.refresh_from_db()
        assert billing_profile.vat_id == ""
        assert billing_profile.vat_country_code == ""
        assert billing_profile.vat_id_validated is False
        assert billing_profile.vat_id_validated_at is None
        assert billing_profile.vies_request_identifier == ""

    def test_returns_404_when_no_profile(self, auth_client: Client) -> None:
        """Returns 404 when user has no billing profile."""
        url = reverse("api:delete_billing_vat_id")
        response = auth_client.delete(url)

        assert response.status_code == 404
