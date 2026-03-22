"""VIES VAT ID validation service.

Validates EU VAT identification numbers via the European Commission's
VIES (VAT Information Exchange System) REST API.

This module contains the pure, model-agnostic validation logic and a
generic ``validate_and_update_vat_entity`` function that works with any
model exposing the standard billing fields via ``HasBillingFields``.
"""

import typing as t
from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog
from django.utils import timezone

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


class HasBillingFields(t.Protocol):
    """Protocol for models that have standard VAT/billing fields.

    Both Organization and UserBillingProfile satisfy this interface.
    """

    vat_id: str
    vat_country_code: str
    vat_id_validated: bool
    vat_id_validated_at: datetime | None
    vies_request_identifier: str
    billing_name: str
    billing_address: str

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Persist the entity."""
        ...


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
    normalized = vat_id.strip().upper()
    if len(normalized) < 3:
        raise ValueError(f"Invalid VAT ID format: {vat_id}")

    country_code, vat_number = parse_vat_id(normalized)

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


def validate_and_update_vat_entity(
    entity: HasBillingFields,
    *,
    entity_id: str,
    entity_type: str = "entity",
) -> VIESValidationResult:
    """Validate a VAT ID and update the model fields.

    Generic implementation that works with any model satisfying ``HasBillingFields``.
    On successful validation, auto-fills billing_name, billing_address, and
    vat_country_code from the VIES response if they are currently empty.

    Args:
        entity: Model instance with billing fields (Organization or UserBillingProfile).
        entity_id: ID string for structured logging.
        entity_type: Label for structured logging (e.g., "org", "user").

    Returns:
        The VIESValidationResult.

    Raises:
        VIESUnavailableError: If VIES is unavailable.
        ValueError: If the entity has no VAT ID set.
    """
    if not entity.vat_id:
        raise ValueError(f"{entity_type} has no VAT ID to validate.")

    result = validate_vat_id(entity.vat_id)

    update_fields = ["vat_id_validated", "vat_id_validated_at", "vies_request_identifier", "updated_at"]

    entity.vat_id_validated = result.valid
    entity.vat_id_validated_at = timezone.now()
    entity.vies_request_identifier = result.request_identifier

    if result.valid:
        # Always sync country code from VAT ID prefix
        country_from_vat = entity.vat_id[:2].upper()
        if entity.vat_country_code != country_from_vat:
            entity.vat_country_code = country_from_vat
            update_fields.append("vat_country_code")

        # Auto-fill billing name from VIES response if empty
        if not entity.billing_name and result.name:
            name = result.name.strip()
            if name and name != "---":
                entity.billing_name = name
                update_fields.append("billing_name")

        # Auto-fill billing address from VIES response if empty
        if not entity.billing_address and result.address:
            # VIES sometimes returns "---" for unavailable addresses
            address = result.address.strip()
            if address and address != "---":
                entity.billing_address = address
                update_fields.append("billing_address")

        logger.info("vat_id_validated", vat_id=entity.vat_id, **{f"{entity_type}_id": entity_id})
    else:
        logger.warning("vat_id_invalid", vat_id=entity.vat_id, **{f"{entity_type}_id": entity_id})

    entity.save(update_fields=update_fields)

    return result
