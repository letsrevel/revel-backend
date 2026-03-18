"""VIES VAT ID validation service.

Validates EU VAT identification numbers via the European Commission's
VIES (VAT Information Exchange System) REST API.

Handles:
- Synchronous validation when VIES is available
- Graceful degradation when VIES is down (saves as pending)
- Async retry via Celery for failed validations
- Auto-fill billing details from VIES response
"""

import typing as t
from dataclasses import dataclass

import httpx
import structlog
from django.utils import timezone

if t.TYPE_CHECKING:
    from events.models.organization import Organization

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


def _parse_vat_id(vat_id: str) -> tuple[str, str]:
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

    country_code, vat_number = _parse_vat_id(vat_id)

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


def validate_and_update_organization(org: "Organization") -> VIESValidationResult:
    """Validate an organization's VAT ID and update the model fields.

    On successful validation, auto-fills billing_address and vat_country_code
    from the VIES response if they are currently empty.

    Args:
        org: Organization with a vat_id to validate.

    Returns:
        The VIESValidationResult.

    Raises:
        VIESUnavailableError: If VIES is unavailable (org is left as pending).
        ValueError: If the org has no VAT ID set.
    """
    if not org.vat_id:
        raise ValueError("Organization has no VAT ID to validate.")

    result = validate_vat_id(org.vat_id)

    update_fields = ["vat_id_validated", "vat_id_validated_at", "vies_request_identifier", "updated_at"]

    org.vat_id_validated = result.valid
    org.vat_id_validated_at = timezone.now()
    org.vies_request_identifier = result.request_identifier

    if result.valid:
        # Always sync country code from VAT ID prefix
        country_from_vat = org.vat_id[:2].upper()
        if org.vat_country_code != country_from_vat:
            org.vat_country_code = country_from_vat
            update_fields.append("vat_country_code")

        # Auto-fill billing name from VIES response if empty
        if not org.billing_name and result.name:
            name = result.name.strip()
            if name and name != "---":
                org.billing_name = name
                update_fields.append("billing_name")

        # Auto-fill billing address from VIES response if empty
        if not org.billing_address and result.address:
            # VIES sometimes returns "---" for unavailable addresses
            address = result.address.strip()
            if address and address != "---":
                org.billing_address = address
                update_fields.append("billing_address")

        logger.info("vat_id_validated", vat_id=org.vat_id, org_id=str(org.id))
    else:
        logger.warning("vat_id_invalid", vat_id=org.vat_id, org_id=str(org.id))

    org.save(update_fields=update_fields)

    return result
