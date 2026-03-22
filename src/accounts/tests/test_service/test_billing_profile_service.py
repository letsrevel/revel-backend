"""Tests for the user billing profile VIES validation service."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser, UserBillingProfile
from accounts.service.billing_profile_service import validate_and_update_billing_profile
from common.service.vies_service import VIESUnavailableError

pytestmark = pytest.mark.django_db


@pytest.fixture
def profile_owner(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="billing_user",
        email="billing_user@example.com",
        password="pass",
    )


@pytest.fixture
def profile_with_vat(profile_owner: RevelUser) -> UserBillingProfile:
    return UserBillingProfile.objects.create(
        user=profile_owner,
        billing_name="",
        vat_id="IT12345678901",
        vat_country_code="",
        billing_address="",
    )


@pytest.fixture
def profile_without_vat(profile_owner: RevelUser) -> UserBillingProfile:
    return UserBillingProfile.objects.create(
        user=profile_owner,
        billing_name="John Doe",
        vat_id="",
    )


def _mock_vies_response(
    *,
    valid: bool = True,
    name: str = "ACME SRL",
    address: str = "VIA ROMA 1, 00100 ROMA RM",
    request_identifier: str = "WAPIAAAAYeBtPMia",
    status_code: int = 200,
) -> MagicMock:
    data: dict[str, t.Any] = {
        "valid": valid,
        "name": name,
        "address": address,
        "requestIdentifier": request_identifier,
    }
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = data
    response.text = str(data)
    return response


class TestValidateAndUpdateBillingProfile:
    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_updates_fields(self, mock_post: MagicMock, profile_with_vat: UserBillingProfile) -> None:
        """Valid VIES response sets vat_id_validated=True and stores metadata."""
        mock_post.return_value = _mock_vies_response(valid=True, request_identifier="REQ789")

        result = validate_and_update_billing_profile(profile_with_vat)

        assert result.valid is True
        profile_with_vat.refresh_from_db()
        assert profile_with_vat.vat_id_validated is True
        assert profile_with_vat.vat_id_validated_at is not None
        assert profile_with_vat.vies_request_identifier == "REQ789"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_syncs_country_code(self, mock_post: MagicMock, profile_with_vat: UserBillingProfile) -> None:
        """On valid result, vat_country_code is set from the VAT ID prefix."""
        assert profile_with_vat.vat_country_code == ""
        mock_post.return_value = _mock_vies_response(valid=True)

        validate_and_update_billing_profile(profile_with_vat)

        profile_with_vat.refresh_from_db()
        assert profile_with_vat.vat_country_code == "IT"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_auto_fills_billing_name(
        self, mock_post: MagicMock, profile_with_vat: UserBillingProfile
    ) -> None:
        """When billing_name is empty, it is auto-filled from the VIES response."""
        assert profile_with_vat.billing_name == ""
        mock_post.return_value = _mock_vies_response(valid=True, name="ACME SRL")

        validate_and_update_billing_profile(profile_with_vat)

        profile_with_vat.refresh_from_db()
        assert profile_with_vat.billing_name == "ACME SRL"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_does_not_overwrite_existing_billing_name(
        self, mock_post: MagicMock, profile_with_vat: UserBillingProfile
    ) -> None:
        """When billing_name is already set, it is NOT overwritten."""
        profile_with_vat.billing_name = "My Legal Entity"
        profile_with_vat.save(update_fields=["billing_name"])
        mock_post.return_value = _mock_vies_response(valid=True, name="ACME SRL")

        validate_and_update_billing_profile(profile_with_vat)

        profile_with_vat.refresh_from_db()
        assert profile_with_vat.billing_name == "My Legal Entity"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_auto_fills_billing_address(
        self, mock_post: MagicMock, profile_with_vat: UserBillingProfile
    ) -> None:
        """When billing_address is empty, it is auto-filled from the VIES response."""
        assert profile_with_vat.billing_address == ""
        mock_post.return_value = _mock_vies_response(valid=True, address="VIA ROMA 1, 00100 ROMA RM")

        validate_and_update_billing_profile(profile_with_vat)

        profile_with_vat.refresh_from_db()
        assert profile_with_vat.billing_address == "VIA ROMA 1, 00100 ROMA RM"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_does_not_overwrite_existing_address(
        self, mock_post: MagicMock, profile_with_vat: UserBillingProfile
    ) -> None:
        """When billing_address is already set, it is NOT overwritten."""
        profile_with_vat.billing_address = "My Custom Address"
        profile_with_vat.save(update_fields=["billing_address"])
        mock_post.return_value = _mock_vies_response(valid=True, address="VIA ROMA 1")

        validate_and_update_billing_profile(profile_with_vat)

        profile_with_vat.refresh_from_db()
        assert profile_with_vat.billing_address == "My Custom Address"

    @patch("common.service.vies_service.httpx.post")
    def test_dash_name_not_filled(self, mock_post: MagicMock, profile_with_vat: UserBillingProfile) -> None:
        """VIES name of '---' is treated as unavailable and not auto-filled."""
        mock_post.return_value = _mock_vies_response(valid=True, name="---")

        validate_and_update_billing_profile(profile_with_vat)

        profile_with_vat.refresh_from_db()
        assert profile_with_vat.billing_name == ""

    @patch("common.service.vies_service.httpx.post")
    def test_dash_address_not_filled(self, mock_post: MagicMock, profile_with_vat: UserBillingProfile) -> None:
        """VIES address of '---' is treated as unavailable and not auto-filled."""
        mock_post.return_value = _mock_vies_response(valid=True, address="---")

        validate_and_update_billing_profile(profile_with_vat)

        profile_with_vat.refresh_from_db()
        assert profile_with_vat.billing_address == ""

    @patch("common.service.vies_service.httpx.post")
    def test_invalid_result_sets_validated_false(
        self, mock_post: MagicMock, profile_with_vat: UserBillingProfile
    ) -> None:
        """Invalid VIES response sets vat_id_validated=False."""
        mock_post.return_value = _mock_vies_response(valid=False)

        result = validate_and_update_billing_profile(profile_with_vat)

        assert result.valid is False
        profile_with_vat.refresh_from_db()
        assert profile_with_vat.vat_id_validated is False

    @patch("common.service.vies_service.httpx.post")
    def test_invalid_result_does_not_fill_fields(
        self, mock_post: MagicMock, profile_with_vat: UserBillingProfile
    ) -> None:
        """Invalid result does not auto-fill billing fields."""
        mock_post.return_value = _mock_vies_response(valid=False, name="ACME", address="Some Address")

        validate_and_update_billing_profile(profile_with_vat)

        profile_with_vat.refresh_from_db()
        assert profile_with_vat.billing_name == ""
        assert profile_with_vat.billing_address == ""
        assert profile_with_vat.vat_country_code == ""

    def test_no_vat_id_raises_value_error(self, profile_without_vat: UserBillingProfile) -> None:
        """Profile with no VAT ID raises ValueError."""
        with pytest.raises(ValueError, match="Billing profile has no VAT ID to validate"):
            validate_and_update_billing_profile(profile_without_vat)

    @patch("common.service.vies_service.httpx.post")
    def test_vies_unavailable_propagates(self, mock_post: MagicMock, profile_with_vat: UserBillingProfile) -> None:
        """VIESUnavailableError propagates to the caller."""
        import httpx

        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(VIESUnavailableError):
            validate_and_update_billing_profile(profile_with_vat)

    @patch("common.service.vies_service.httpx.post")
    def test_vies_unavailable_does_not_update_profile(
        self, mock_post: MagicMock, profile_with_vat: UserBillingProfile
    ) -> None:
        """When VIES is unavailable, the profile is not modified in the DB."""
        import httpx

        original_validated = profile_with_vat.vat_id_validated
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(VIESUnavailableError):
            validate_and_update_billing_profile(profile_with_vat)

        profile_with_vat.refresh_from_db()
        assert profile_with_vat.vat_id_validated == original_validated

    @patch("common.service.vies_service.httpx.post")
    def test_validated_at_is_set_to_current_time(
        self, mock_post: MagicMock, profile_with_vat: UserBillingProfile
    ) -> None:
        """vat_id_validated_at is set to approximately the current time."""
        mock_post.return_value = _mock_vies_response(valid=True)
        before = timezone.now()

        validate_and_update_billing_profile(profile_with_vat)

        after = timezone.now()
        profile_with_vat.refresh_from_db()
        assert before <= profile_with_vat.vat_id_validated_at <= after  # type: ignore[operator]
