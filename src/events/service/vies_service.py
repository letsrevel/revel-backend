"""Organization-specific VIES VAT ID validation.

Wraps the common VIES validation service with organization model updates:
- Auto-fill billing details from VIES response
"""

import typing as t

import structlog
from django.utils import timezone

from common.service.vies_service import VIESValidationResult, validate_vat_id

if t.TYPE_CHECKING:
    from events.models.organization import Organization

logger = structlog.get_logger(__name__)


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
