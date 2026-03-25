"""Tests for validate_vat_id_cached() — caching behaviour.

Tests cover:
- Cache hit returns cached result without calling VIES
- Cache miss calls VIES and stores the result
- VIESUnavailableError is NOT cached and is re-raised
"""

from unittest.mock import MagicMock, patch

import pytest

from common.service.vies_service import (
    VIES_CACHE_TTL,
    VIESUnavailableError,
    VIESValidationResult,
    validate_vat_id_cached,
)

pytestmark = pytest.mark.django_db


MOCK_CACHE_GET = "common.service.vies_service.cache.get"
MOCK_CACHE_SET = "common.service.vies_service.cache.set"
MOCK_VALIDATE = "common.service.vies_service.validate_vat_id"


class TestValidateVatIdCached:
    """Test Redis-cached VIES validation wrapper."""

    @patch(MOCK_CACHE_GET)
    @patch(MOCK_VALIDATE)
    def test_cache_hit_returns_cached_result(self, mock_validate: MagicMock, mock_cache_get: MagicMock) -> None:
        """When the cache has a hit, return it without calling VIES API."""
        cached_data = {
            "valid": True,
            "name": "Cached Corp",
            "address": "Cache Street 1",
            "request_identifier": "CACHED123",
        }
        mock_cache_get.return_value = cached_data

        result = validate_vat_id_cached("IT12345678901")

        assert result == VIESValidationResult(
            valid=True, name="Cached Corp", address="Cache Street 1", request_identifier="CACHED123"
        )
        mock_validate.assert_not_called()

    @patch(MOCK_CACHE_SET)
    @patch(MOCK_VALIDATE)
    @patch(MOCK_CACHE_GET, return_value=None)
    def test_cache_miss_calls_vies_and_caches(
        self,
        mock_cache_get: MagicMock,
        mock_validate: MagicMock,
        mock_cache_set: MagicMock,
    ) -> None:
        """On cache miss, validate via VIES and store the result."""
        vies_result = VIESValidationResult(
            valid=True,
            name="Fresh Corp",
            address="Fresh Street 1",
            request_identifier="FRESH123",
        )
        mock_validate.return_value = vies_result

        result = validate_vat_id_cached("IT12345678901")

        assert result == vies_result
        mock_validate.assert_called_once_with("IT12345678901")
        mock_cache_set.assert_called_once()

        # Verify cache key and TTL
        call_args = mock_cache_set.call_args
        assert call_args[0][0] == "vies:validation:IT12345678901"
        assert call_args[1]["timeout"] == VIES_CACHE_TTL

    @patch(MOCK_CACHE_SET)
    @patch(MOCK_VALIDATE)
    @patch(MOCK_CACHE_GET, return_value=None)
    def test_vies_unavailable_is_not_cached(
        self,
        mock_cache_get: MagicMock,
        mock_validate: MagicMock,
        mock_cache_set: MagicMock,
    ) -> None:
        """VIESUnavailableError should NOT be cached and should be re-raised."""
        mock_validate.side_effect = VIESUnavailableError("VIES is down")

        with pytest.raises(VIESUnavailableError, match="VIES is down"):
            validate_vat_id_cached("IT12345678901")

        mock_cache_set.assert_not_called()

    @patch(MOCK_CACHE_SET)
    @patch(MOCK_VALIDATE)
    @patch(MOCK_CACHE_GET, return_value=None)
    def test_value_error_is_not_cached(
        self,
        mock_cache_get: MagicMock,
        mock_validate: MagicMock,
        mock_cache_set: MagicMock,
    ) -> None:
        """ValueError (invalid format) should NOT be cached and should be re-raised."""
        mock_validate.side_effect = ValueError("Invalid VAT ID format")

        with pytest.raises(ValueError, match="Invalid VAT ID format"):
            validate_vat_id_cached("XY")

        mock_cache_set.assert_not_called()

    @patch(MOCK_CACHE_GET)
    @patch(MOCK_VALIDATE)
    def test_normalizes_vat_id_for_cache_key(self, mock_validate: MagicMock, mock_cache_get: MagicMock) -> None:
        """VAT IDs with whitespace/lowercase should produce the same cache key."""
        mock_cache_get.return_value = None
        mock_validate.return_value = VIESValidationResult(valid=True, name="Test", address="", request_identifier="R1")

        # Call with messy input
        validate_vat_id_cached("  it 1234 5678 901  ")

        # Check the cache key used — should be normalized
        cache_key = mock_cache_get.call_args[0][0]
        assert cache_key == "vies:validation:IT12345678901"

    @patch(MOCK_CACHE_SET)
    @patch(MOCK_VALIDATE)
    @patch(MOCK_CACHE_GET, return_value=None)
    def test_invalid_vat_result_is_cached(
        self,
        mock_cache_get: MagicMock,
        mock_validate: MagicMock,
        mock_cache_set: MagicMock,
    ) -> None:
        """An invalid (but successfully queried) result should be cached."""
        vies_result = VIESValidationResult(
            valid=False,
            name="",
            address="",
            request_identifier="INV123",
        )
        mock_validate.return_value = vies_result

        result = validate_vat_id_cached("IT00000000000")

        assert result.valid is False
        mock_cache_set.assert_called_once()
