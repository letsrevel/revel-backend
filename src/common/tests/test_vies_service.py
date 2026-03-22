"""Tests for the common VIES VAT ID validation service.

Tests cover the pure, model-agnostic validation functions extracted from events.
"""

import typing as t
from unittest.mock import MagicMock, patch

import pytest

from common.service.vies_service import (
    VIES_REST_URL,
    VIES_TIMEOUT_SECONDS,
    VIESUnavailableError,
    VIESValidationResult,
    parse_vat_id,
    validate_vat_id,
)


def _mock_vies_response(
    *,
    valid: bool = True,
    name: str = "ACME SRL",
    address: str = "VIA ROMA 1",
    request_identifier: str = "REQ123",
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


class TestParseVatId:
    def test_standard_eu_vat_id(self) -> None:
        country, number = parse_vat_id("IT12345678901")
        assert country == "IT"
        assert number == "12345678901"

    def test_whitespace_is_stripped(self) -> None:
        country, number = parse_vat_id("  DE123456789  ")
        assert country == "DE"
        assert number == "123456789"

    def test_lowercase_is_uppercased(self) -> None:
        country, number = parse_vat_id("fr12345678901")
        assert country == "FR"
        assert number == "12345678901"

    def test_country_code_with_special_chars(self) -> None:
        country, number = parse_vat_id("ATU12345678")
        assert country == "AT"
        assert number == "U12345678"


class TestValidateVatId:
    @patch("common.service.vies_service.httpx.post")
    def test_valid_vat_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_vies_response(valid=True, request_identifier="REQ123")

        result = validate_vat_id("IT12345678901")

        assert result == VIESValidationResult(
            valid=True, name="ACME SRL", address="VIA ROMA 1", request_identifier="REQ123"
        )

    @patch("common.service.vies_service.httpx.post")
    def test_invalid_vat_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_vies_response(valid=False, name="", address="", request_identifier="REQ456")

        result = validate_vat_id("IT00000000000")

        assert result.valid is False

    @patch("common.service.vies_service.httpx.post")
    def test_correct_api_payload(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_vies_response()

        validate_vat_id("DE123456789")

        mock_post.assert_called_once_with(
            VIES_REST_URL,
            json={"countryCode": "DE", "vatNumber": "123456789"},
            timeout=VIES_TIMEOUT_SECONDS,
        )

    def test_short_vat_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid VAT ID format"):
            validate_vat_id("IT")

    def test_empty_vat_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid VAT ID format"):
            validate_vat_id("")

    @patch("common.service.vies_service.httpx.post")
    def test_network_error_raises_vies_unavailable(self, mock_post: MagicMock) -> None:
        import httpx

        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(VIESUnavailableError, match="VIES service unreachable"):
            validate_vat_id("IT12345678901")

    @patch("common.service.vies_service.httpx.post")
    def test_http_500_raises_vies_unavailable(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_vies_response(status_code=500)

        with pytest.raises(VIESUnavailableError, match="VIES returned HTTP 500"):
            validate_vat_id("IT12345678901")

    @patch("common.service.vies_service.httpx.post")
    def test_unexpected_response_raises_vies_unavailable(self, mock_post: MagicMock) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"error": "INVALID_INPUT"}
        mock_post.return_value = response

        with pytest.raises(VIESUnavailableError, match="Unexpected VIES response format"):
            validate_vat_id("IT12345678901")


class TestEUMemberStatesConstant:
    def test_eu_member_states_has_27_members(self) -> None:
        from common.constants import EU_MEMBER_STATES

        assert len(EU_MEMBER_STATES) == 27

    def test_eu_member_states_contains_known_members(self) -> None:
        from common.constants import EU_MEMBER_STATES

        for code in ["IT", "DE", "FR", "ES", "NL", "PL"]:
            assert code in EU_MEMBER_STATES

    def test_eu_member_states_excludes_non_members(self) -> None:
        from common.constants import EU_MEMBER_STATES

        for code in ["US", "GB", "CH", "NO"]:
            assert code not in EU_MEMBER_STATES
