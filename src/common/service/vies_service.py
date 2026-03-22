"""VIES VAT ID validation service.

Validates EU VAT identification numbers via the European Commission's
VIES (VAT Information Exchange System) REST API.

This module contains the pure, model-agnostic validation logic.
App-specific update functions (e.g., for Organization or UserBillingProfile)
live in their respective apps.
"""

from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger(__name__)

VIES_REST_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
VIES_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class VIESValidationResult:
    """Result of a VIES VAT ID validation."""

    valid: bool
    name: str
    address: str
    request_identifier: str


class VIESUnavailableError(Exception):
    """Raised when the VIES service is unavailable."""


def parse_vat_id(vat_id: str) -> tuple[str, str]:
    """Split a full VAT ID into country code and number.

    Args:
        vat_id: Full VAT ID with country prefix (e.g., "IT12345678901").

    Returns:
        Tuple of (country_code, vat_number).
    """
    vat_id = vat_id.strip().upper()
    return vat_id[:2], vat_id[2:]


def validate_vat_id(vat_id: str) -> VIESValidationResult:
    """Validate a VAT ID against the VIES REST API.

    Args:
        vat_id: Full VAT ID with country prefix (e.g., "IT12345678901").

    Returns:
        VIESValidationResult with validation details.

    Raises:
        VIESUnavailableError: If the VIES service is unreachable or returns an error.
        ValueError: If the VAT ID format is invalid.
    """
    if len(vat_id) < 3:
        raise ValueError(f"Invalid VAT ID format: {vat_id}")

    country_code, vat_number = parse_vat_id(vat_id)

    try:
        response = httpx.post(
            VIES_REST_URL,
            json={
                "countryCode": country_code,
                "vatNumber": vat_number,
            },
            timeout=VIES_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise VIESUnavailableError(f"VIES service unreachable: {e}") from e

    if response.status_code != 200:
        raise VIESUnavailableError(f"VIES returned HTTP {response.status_code}: {response.text}")

    data = response.json()

    if "valid" not in data:
        raise VIESUnavailableError(f"Unexpected VIES response format: {data}")

    return VIESValidationResult(
        valid=data["valid"],
        name=data.get("name", ""),
        address=data.get("address", ""),
        request_identifier=data.get("requestIdentifier", ""),
    )
