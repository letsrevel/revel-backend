"""Tests for the VIES VAT ID validation service.

Tests cover:
- VAT ID parsing (country code extraction, whitespace handling, case normalization)
- VIES REST API integration (valid, invalid, unavailable, unexpected responses)
- Organization update flow (field updates, address auto-fill, country code sync)
- Error handling (missing VAT ID, network errors, non-200 responses)
"""

from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from common.service.vies_service import (
    VIES_REST_URL,
    VIES_TIMEOUT_SECONDS,
    VIESUnavailableError,
    VIESValidationResult,
    parse_vat_id,
    validate_vat_id,
)
from common.tests.vies_test_utils import mock_vies_response
from events.models import Organization
from events.service.vies_service import validate_and_update_organization

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org_owner(django_user_model: type[RevelUser]) -> RevelUser:
    """User who owns the organization used in VIES tests."""
    return django_user_model.objects.create_user(
        username="vies_owner",
        email="vies_owner@example.com",
        password="pass",
    )


@pytest.fixture
def org_with_vat(org_owner: RevelUser) -> Organization:
    """Organization with a VAT ID set, ready for VIES validation."""
    return Organization.objects.create(
        name="VIES Test Org",
        slug="vies-test-org",
        owner=org_owner,
        vat_id="IT12345678901",
        vat_country_code="",
        billing_address="",
    )


@pytest.fixture
def org_without_vat(org_owner: RevelUser) -> Organization:
    """Organization with no VAT ID set."""
    return Organization.objects.create(
        name="No VAT Org",
        slug="no-vat-org",
        owner=org_owner,
        vat_id="",
    )


# ===========================================================================
# parse_vat_id
# ===========================================================================


class TestParseVatId:
    """Tests for the parse_vat_id helper."""

    def test_standard_eu_vat_id(self) -> None:
        """Standard EU VAT ID is split into country code and number."""
        country, number = parse_vat_id("IT12345678901")

        assert country == "IT"
        assert number == "12345678901"

    def test_whitespace_is_stripped(self) -> None:
        """Leading/trailing whitespace is removed before parsing."""
        country, number = parse_vat_id("  DE123456789  ")

        assert country == "DE"
        assert number == "123456789"

    def test_lowercase_is_uppercased(self) -> None:
        """Lowercase input is converted to uppercase."""
        country, number = parse_vat_id("fr12345678901")

        assert country == "FR"
        assert number == "12345678901"

    def test_mixed_case_is_uppercased(self) -> None:
        """Mixed case input is normalized to uppercase."""
        country, number = parse_vat_id("eS12345678A")

        assert country == "ES"
        assert number == "12345678A"

    def test_country_code_with_special_chars_in_number(self) -> None:
        """VAT numbers containing non-digit characters after prefix are preserved."""
        country, number = parse_vat_id("ATU12345678")

        assert country == "AT"
        assert number == "U12345678"


# ===========================================================================
# validate_vat_id
# ===========================================================================


class TestValidateVatId:
    """Tests for the validate_vat_id function (VIES API integration)."""

    @patch("common.service.vies_service.httpx.post")
    def test_valid_vat_id_returns_valid_result(self, mock_post: MagicMock) -> None:
        """A valid VAT ID returns a VIESValidationResult with valid=True and details."""
        mock_post.return_value = mock_vies_response(
            valid=True,
            name="ACME SRL",
            address="VIA ROMA 1",
            request_identifier="REQ123",
        )

        result = validate_vat_id("IT12345678901")

        assert result == VIESValidationResult(
            valid=True,
            name="ACME SRL",
            address="VIA ROMA 1",
            request_identifier="REQ123",
        )

    @patch("common.service.vies_service.httpx.post")
    def test_invalid_vat_id_returns_invalid_result(self, mock_post: MagicMock) -> None:
        """An invalid VAT ID returns a VIESValidationResult with valid=False."""
        mock_post.return_value = mock_vies_response(
            valid=False,
            name="",
            address="",
            request_identifier="REQ456",
        )

        result = validate_vat_id("IT00000000000")

        assert result.valid is False
        assert result.name == ""
        assert result.address == ""
        assert result.request_identifier == "REQ456"

    @patch("common.service.vies_service.httpx.post")
    def test_correct_api_payload_is_sent(self, mock_post: MagicMock) -> None:
        """The VIES API receives the correct countryCode and vatNumber split."""
        mock_post.return_value = mock_vies_response()

        validate_vat_id("DE123456789")

        mock_post.assert_called_once_with(
            VIES_REST_URL,
            json={
                "countryCode": "DE",
                "vatNumber": "123456789",
            },
            timeout=VIES_TIMEOUT_SECONDS,
        )

    @patch("common.service.vies_service.httpx.post")
    def test_lowercase_vat_id_sends_uppercased_payload(self, mock_post: MagicMock) -> None:
        """Lowercase VAT IDs are uppercased before sending to VIES."""
        mock_post.return_value = mock_vies_response()

        validate_vat_id("fr12345678901")

        mock_post.assert_called_once_with(
            VIES_REST_URL,
            json={
                "countryCode": "FR",
                "vatNumber": "12345678901",
            },
            timeout=VIES_TIMEOUT_SECONDS,
        )

    @patch("common.service.vies_service.httpx.post")
    def test_missing_optional_fields_default_to_empty_string(self, mock_post: MagicMock) -> None:
        """When VIES response lacks optional fields, they default to empty strings."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"valid": True}
        mock_post.return_value = response

        result = validate_vat_id("IT12345678901")

        assert result.valid is True
        assert result.name == ""
        assert result.address == ""
        assert result.request_identifier == ""

    def test_short_vat_id_raises_value_error(self) -> None:
        """VAT IDs shorter than 3 characters raise ValueError."""
        with pytest.raises(ValueError, match="Invalid VAT ID format"):
            validate_vat_id("IT")

    def test_single_char_vat_id_raises_value_error(self) -> None:
        """Single character VAT ID raises ValueError."""
        with pytest.raises(ValueError, match="Invalid VAT ID format"):
            validate_vat_id("I")

    def test_empty_vat_id_raises_value_error(self) -> None:
        """Empty VAT ID string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid VAT ID format"):
            validate_vat_id("")

    @patch("common.service.vies_service.httpx.post")
    def test_network_error_raises_vies_unavailable(self, mock_post: MagicMock) -> None:
        """Network errors (timeouts, DNS failures) raise VIESUnavailableError."""
        import httpx

        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(VIESUnavailableError, match="VIES service unreachable"):
            validate_vat_id("IT12345678901")

    @patch("common.service.vies_service.httpx.post")
    def test_timeout_raises_vies_unavailable(self, mock_post: MagicMock) -> None:
        """Request timeouts raise VIESUnavailableError."""
        import httpx

        mock_post.side_effect = httpx.ReadTimeout("Read timed out")

        with pytest.raises(VIESUnavailableError, match="VIES service unreachable"):
            validate_vat_id("IT12345678901")

    @patch("common.service.vies_service.httpx.post")
    def test_http_500_raises_vies_unavailable(self, mock_post: MagicMock) -> None:
        """Non-200 HTTP responses raise VIESUnavailableError with status code."""
        mock_post.return_value = mock_vies_response(status_code=500)

        with pytest.raises(VIESUnavailableError, match="VIES returned HTTP 500"):
            validate_vat_id("IT12345678901")

    @patch("common.service.vies_service.httpx.post")
    def test_http_503_raises_vies_unavailable(self, mock_post: MagicMock) -> None:
        """HTTP 503 Service Unavailable raises VIESUnavailableError."""
        response = MagicMock()
        response.status_code = 503
        response.text = "Service Unavailable"
        mock_post.return_value = response

        with pytest.raises(VIESUnavailableError, match="VIES returned HTTP 503"):
            validate_vat_id("IT12345678901")

    @patch("common.service.vies_service.httpx.post")
    def test_unexpected_response_without_valid_key_raises_vies_unavailable(self, mock_post: MagicMock) -> None:
        """Responses missing the 'valid' key raise VIESUnavailableError."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"error": "INVALID_INPUT", "message": "bad request"}
        mock_post.return_value = response

        with pytest.raises(VIESUnavailableError, match="Unexpected VIES response format"):
            validate_vat_id("IT12345678901")

    @patch("common.service.vies_service.httpx.post")
    def test_empty_json_response_raises_vies_unavailable(self, mock_post: MagicMock) -> None:
        """An empty JSON object in the response raises VIESUnavailableError."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {}
        mock_post.return_value = response

        with pytest.raises(VIESUnavailableError, match="Unexpected VIES response format"):
            validate_vat_id("IT12345678901")


# ===========================================================================
# validate_and_update_organization
# ===========================================================================


class TestValidateAndUpdateOrganization:
    """Tests for the validate_and_update_organization function."""

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_updates_org_fields(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """Valid VIES response sets vat_id_validated=True and stores metadata."""
        mock_post.return_value = mock_vies_response(
            valid=True,
            request_identifier="REQ789",
        )

        result = validate_and_update_organization(org_with_vat)

        assert result.valid is True

        org_with_vat.refresh_from_db()
        assert org_with_vat.vat_id_validated is True
        assert org_with_vat.vat_id_validated_at is not None
        assert org_with_vat.vies_request_identifier == "REQ789"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_syncs_country_code_from_vat_prefix(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """On valid result, vat_country_code is set from the VAT ID prefix."""
        assert org_with_vat.vat_country_code == ""  # starts empty
        mock_post.return_value = mock_vies_response(valid=True)

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.vat_country_code == "IT"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_does_not_overwrite_existing_country_code(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """When the country code already matches, it stays the same (no-op)."""
        org_with_vat.vat_country_code = "IT"
        org_with_vat.save(update_fields=["vat_country_code"])
        mock_post.return_value = mock_vies_response(valid=True)

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.vat_country_code == "IT"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_auto_fills_empty_billing_address(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """When billing_address is empty, it is auto-filled from the VIES response."""
        assert org_with_vat.billing_address == ""  # starts empty
        mock_post.return_value = mock_vies_response(
            valid=True,
            address="VIA ROMA 1, 00100 ROMA RM",
        )

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_address == "VIA ROMA 1, 00100 ROMA RM"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_does_not_overwrite_existing_billing_address(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """When billing_address is already set, it is NOT overwritten by VIES."""
        org_with_vat.billing_address = "My Custom Address"
        org_with_vat.save(update_fields=["billing_address"])
        mock_post.return_value = mock_vies_response(
            valid=True,
            address="VIA ROMA 1, 00100 ROMA RM",
        )

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_address == "My Custom Address"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_with_dash_address_does_not_fill(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """VIES address of '---' is treated as unavailable and not auto-filled."""
        assert org_with_vat.billing_address == ""
        mock_post.return_value = mock_vies_response(
            valid=True,
            address="---",
        )

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_address == ""

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_with_whitespace_only_address_does_not_fill(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """VIES address that is only whitespace is treated as empty and not filled."""
        assert org_with_vat.billing_address == ""
        mock_post.return_value = mock_vies_response(
            valid=True,
            address="   ",
        )

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_address == ""

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_with_empty_address_does_not_fill(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """Empty VIES address string does not change billing_address."""
        assert org_with_vat.billing_address == ""
        mock_post.return_value = mock_vies_response(valid=True, address="")

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_address == ""

    @patch("common.service.vies_service.httpx.post")
    def test_invalid_result_sets_validated_false(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """Invalid VIES response sets vat_id_validated=False."""
        mock_post.return_value = mock_vies_response(valid=False)

        result = validate_and_update_organization(org_with_vat)

        assert result.valid is False

        org_with_vat.refresh_from_db()
        assert org_with_vat.vat_id_validated is False
        assert org_with_vat.vat_id_validated_at is not None

    @patch("common.service.vies_service.httpx.post")
    def test_invalid_result_does_not_update_country_code(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """Invalid result does not sync the country code from the VAT prefix."""
        assert org_with_vat.vat_country_code == ""
        mock_post.return_value = mock_vies_response(valid=False)

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.vat_country_code == ""

    @patch("common.service.vies_service.httpx.post")
    def test_invalid_result_does_not_fill_billing_address(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """Invalid result does not auto-fill billing_address from VIES."""
        assert org_with_vat.billing_address == ""
        mock_post.return_value = mock_vies_response(valid=False, address="Some Address")

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_address == ""

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_auto_fills_empty_billing_name(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """When billing_name is empty, it is auto-filled from the VIES response."""
        assert org_with_vat.billing_name == ""
        mock_post.return_value = mock_vies_response(valid=True, name="ACME SRL")

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_name == "ACME SRL"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_does_not_overwrite_existing_billing_name(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """When billing_name is already set, it is NOT overwritten by VIES."""
        org_with_vat.billing_name = "My Legal Entity"
        org_with_vat.save(update_fields=["billing_name"])
        mock_post.return_value = mock_vies_response(valid=True, name="ACME SRL")

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_name == "My Legal Entity"

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_with_dash_name_does_not_fill(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """VIES name of '---' is treated as unavailable and not auto-filled."""
        assert org_with_vat.billing_name == ""
        mock_post.return_value = mock_vies_response(valid=True, name="---")

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_name == ""

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_with_empty_name_does_not_fill(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """Empty VIES name string does not change billing_name."""
        assert org_with_vat.billing_name == ""
        mock_post.return_value = mock_vies_response(valid=True, name="")

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_name == ""

    @patch("common.service.vies_service.httpx.post")
    def test_valid_result_with_whitespace_only_name_does_not_fill(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """VIES name that is only whitespace is treated as empty and not filled."""
        assert org_with_vat.billing_name == ""
        mock_post.return_value = mock_vies_response(valid=True, name="   ")

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_name == ""

    @patch("common.service.vies_service.httpx.post")
    def test_invalid_result_does_not_fill_billing_name(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """Invalid result does not auto-fill billing_name from VIES."""
        assert org_with_vat.billing_name == ""
        mock_post.return_value = mock_vies_response(valid=False, name="ACME SRL")

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.billing_name == ""

    def test_no_vat_id_raises_value_error(self, org_without_vat: Organization) -> None:
        """Organization with no VAT ID raises ValueError."""
        with pytest.raises(ValueError, match="has no VAT ID to validate"):
            validate_and_update_organization(org_without_vat)

    @patch("common.service.vies_service.httpx.post")
    def test_vies_unavailable_propagates(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """VIESUnavailableError from the API call propagates to the caller."""
        import httpx

        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(VIESUnavailableError):
            validate_and_update_organization(org_with_vat)

    @patch("common.service.vies_service.httpx.post")
    def test_vies_unavailable_does_not_update_org(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """When VIES is unavailable, the organization is not modified in the DB."""
        import httpx

        original_validated = org_with_vat.vat_id_validated
        original_validated_at = org_with_vat.vat_id_validated_at
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(VIESUnavailableError):
            validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.vat_id_validated == original_validated
        assert org_with_vat.vat_id_validated_at == original_validated_at

    @patch("common.service.vies_service.httpx.post")
    def test_validated_at_is_set_to_current_time(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """vat_id_validated_at is set to approximately the current time."""
        mock_post.return_value = mock_vies_response(valid=True)
        before = timezone.now()

        validate_and_update_organization(org_with_vat)

        after = timezone.now()
        org_with_vat.refresh_from_db()
        assert before <= org_with_vat.vat_id_validated_at <= after  # type: ignore[operator]

    @patch("common.service.vies_service.httpx.post")
    def test_returns_vies_validation_result(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """The function returns the VIESValidationResult from the API call."""
        mock_post.return_value = mock_vies_response(
            valid=True,
            name="ACME SRL",
            address="VIA ROMA 1",
            request_identifier="REQ-ABC",
        )

        result = validate_and_update_organization(org_with_vat)

        assert isinstance(result, VIESValidationResult)
        assert result.valid is True
        assert result.name == "ACME SRL"
        assert result.address == "VIA ROMA 1"
        assert result.request_identifier == "REQ-ABC"

    @patch("common.service.vies_service.httpx.post")
    def test_country_code_corrected_when_mismatched(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """If org has wrong vat_country_code, it is corrected from VAT ID prefix."""
        org_with_vat.vat_country_code = "DE"  # Wrong; VAT ID starts with IT
        org_with_vat.save(update_fields=["vat_country_code"])
        mock_post.return_value = mock_vies_response(valid=True)

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.vat_country_code == "IT"

    @patch("common.service.vies_service.httpx.post")
    def test_request_identifier_stored_on_org(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """The VIES request identifier is persisted on the organization for audit trails."""
        mock_post.return_value = mock_vies_response(
            valid=True,
            request_identifier="AUDIT-TRAIL-123",
        )

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.vies_request_identifier == "AUDIT-TRAIL-123"

    @patch("common.service.vies_service.httpx.post")
    def test_previously_validated_org_can_be_revalidated(
        self, mock_post: MagicMock, org_with_vat: Organization
    ) -> None:
        """An org that was previously validated can be re-validated (e.g., monthly re-check)."""
        # First validation
        mock_post.return_value = mock_vies_response(valid=True, request_identifier="FIRST")
        validate_and_update_organization(org_with_vat)
        org_with_vat.refresh_from_db()
        first_validated_at = org_with_vat.vat_id_validated_at

        # Second validation
        mock_post.return_value = mock_vies_response(valid=True, request_identifier="SECOND")
        validate_and_update_organization(org_with_vat)
        org_with_vat.refresh_from_db()

        assert org_with_vat.vies_request_identifier == "SECOND"
        assert org_with_vat.vat_id_validated_at >= first_validated_at  # type: ignore[operator]

    @patch("common.service.vies_service.httpx.post")
    def test_previously_valid_org_can_become_invalid(self, mock_post: MagicMock, org_with_vat: Organization) -> None:
        """An org that was valid can become invalid on re-validation."""
        # Make it valid first
        org_with_vat.vat_id_validated = True
        org_with_vat.save(update_fields=["vat_id_validated"])

        mock_post.return_value = mock_vies_response(valid=False)

        validate_and_update_organization(org_with_vat)

        org_with_vat.refresh_from_db()
        assert org_with_vat.vat_id_validated is False
