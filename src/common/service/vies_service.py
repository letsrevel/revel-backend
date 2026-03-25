"""VIES VAT ID validation and billing field management service.

Provides model-agnostic operations for any entity with standard billing fields
(Organization, UserBillingProfile, etc.) via the ``HasBillingFields`` protocol.

Contains:
- Pure VIES REST API validation (``validate_vat_id``)
- Generic billing field operations (``set_vat_id``, ``clear_vat_fields``,
  ``update_billing_info``, ``validate_and_update_vat_entity``)
"""

import typing as t
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

import httpx
import structlog
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

logger = structlog.get_logger(__name__)

VIES_REST_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
VIES_TIMEOUT_SECONDS = 10

VAT_RESET_FIELDS = [
    "vat_id",
    "vat_country_code",
    "vat_id_validated",
    "vat_id_validated_at",
    "vies_request_identifier",
    "updated_at",
]


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
    billing_email: str

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Persist the entity."""
        ...

    def refresh_from_db(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Reload from database."""
        ...


# ---------------------------------------------------------------------------
# Pure VIES API
# ---------------------------------------------------------------------------


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


VIES_CACHE_TTL = 1800  # 30 minutes


def validate_vat_id_cached(vat_id: str) -> VIESValidationResult:
    """Validate a VAT ID with Redis caching.

    Returns cached result if available. On cache miss, validates via VIES
    and caches the result. VIES unavailability is NOT cached — the error
    is re-raised so callers can handle fallback.

    Args:
        vat_id: Full VAT ID with country prefix.

    Returns:
        VIESValidationResult.

    Raises:
        VIESUnavailableError: If VIES is unavailable (not cached).
        ValueError: If the VAT ID format is invalid.
    """
    normalized = vat_id.strip().upper().replace(" ", "")
    cache_key = f"vies:validation:{normalized}"

    cached = cache.get(cache_key)
    if cached is not None:
        return VIESValidationResult(**cached)

    result = validate_vat_id(vat_id)  # may raise VIESUnavailableError
    cache.set(cache_key, asdict(result), timeout=VIES_CACHE_TTL)
    return result


# ---------------------------------------------------------------------------
# Generic billing field operations
# ---------------------------------------------------------------------------


def validate_and_update_vat_entity(
    entity: HasBillingFields,
    *,
    entity_id: str,
    entity_type: str = "entity",
) -> VIESValidationResult:
    """Validate a VAT ID and update the model fields.

    On successful validation, auto-fills billing_name, billing_address, and
    vat_country_code from the VIES response if they are currently empty.

    Args:
        entity: Model instance with billing fields.
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
        country_from_vat = entity.vat_id[:2].upper()
        if entity.vat_country_code != country_from_vat:
            entity.vat_country_code = country_from_vat
            update_fields.append("vat_country_code")

        if not entity.billing_name and result.name:
            name = result.name.strip()
            if name and name != "---":
                entity.billing_name = name
                update_fields.append("billing_name")

        if not entity.billing_address and result.address:
            address = result.address.strip()
            if address and address != "---":
                entity.billing_address = address
                update_fields.append("billing_address")

        logger.info("vat_id_validated", vat_id=entity.vat_id, **{f"{entity_type}_id": entity_id})
    else:
        logger.warning("vat_id_invalid", vat_id=entity.vat_id, **{f"{entity_type}_id": entity_id})

    entity.save(update_fields=update_fields)

    return result


def set_vat_id(
    entity: HasBillingFields,
    vat_id: str,
    *,
    entity_id: str,
    entity_type: str,
    on_vies_unavailable: Callable[[], None] | None = None,
    rollback_on_invalid: bool = True,
) -> None:
    """Set a VAT ID on an entity, validate via VIES, and handle results.

    Args:
        entity: Model instance with billing fields.
        vat_id: The new VAT ID to set.
        entity_id: ID string for structured logging.
        entity_type: Label for structured logging.
        on_vies_unavailable: Optional callback invoked when VIES is down
            (e.g., to queue a background retry task). Called before raising HttpError.
        rollback_on_invalid: If True, clears the VAT ID when VIES returns invalid.

    Raises:
        HttpError 400: If VIES says the VAT ID is invalid.
        HttpError 503: If VIES is unavailable.
    """
    entity.vat_id = vat_id
    entity.vat_country_code = vat_id[:2].upper()
    entity.vat_id_validated = False
    entity.vat_id_validated_at = None
    entity.vies_request_identifier = ""
    entity.save(update_fields=VAT_RESET_FIELDS)

    try:
        result = validate_and_update_vat_entity(entity, entity_id=entity_id, entity_type=entity_type)
        if not result.valid:
            if rollback_on_invalid:
                clear_vat_fields(entity)
            raise HttpError(400, str(_("The VAT ID is not valid according to VIES.")))
    except VIESUnavailableError:
        logger.warning("vies_unavailable", **{f"{entity_type}_id": entity_id})
        if on_vies_unavailable:
            on_vies_unavailable()
        raise HttpError(
            503,
            str(
                _(
                    "VIES validation service is temporarily unavailable."
                    " The VAT ID has been saved. Please try again later."
                )
            ),
        )

    entity.refresh_from_db()


def clear_vat_fields(entity: HasBillingFields) -> None:
    """Clear all VAT-related fields on an entity."""
    entity.vat_id = ""
    entity.vat_country_code = ""
    entity.vat_id_validated = False
    entity.vat_id_validated_at = None
    entity.vies_request_identifier = ""
    entity.save(update_fields=VAT_RESET_FIELDS)


def update_billing_info(entity: HasBillingFields, data: dict[str, t.Any]) -> None:
    """Update billing info fields on an entity.

    Validates that vat_country_code doesn't conflict with an existing VAT ID prefix.

    Raises:
        HttpError 422: If vat_country_code conflicts with existing VAT ID prefix.
    """
    new_country = data.get("vat_country_code")
    if new_country and entity.vat_id:
        vat_prefix = entity.vat_id[:2].upper()
        if new_country != vat_prefix:
            raise HttpError(
                422,
                str(_("Country code must match the VAT ID prefix (%(prefix)s).") % {"prefix": vat_prefix}),
            )

    for field, value in data.items():
        setattr(entity, field, value)
    entity.save(update_fields=[*data.keys(), "updated_at"])
