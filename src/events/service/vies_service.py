"""Organization-specific VIES VAT ID validation.

Thin wrapper around the generic ``validate_and_update_vat_entity`` in common.
"""

import typing as t

from common.service.vies_service import VIESValidationResult, validate_and_update_vat_entity

if t.TYPE_CHECKING:
    from events.models.organization import Organization


def validate_and_update_organization(org: "Organization") -> VIESValidationResult:
    """Validate an organization's VAT ID and update the model fields.

    On successful validation, auto-fills billing_name, billing_address, and
    vat_country_code from the VIES response if they are currently empty.

    Args:
        org: Organization with a vat_id to validate.

    Returns:
        The VIESValidationResult.

    Raises:
        VIESUnavailableError: If VIES is unavailable (org is left as pending).
        ValueError: If the org has no VAT ID set.
    """
    return validate_and_update_vat_entity(org, entity_id=str(org.id), entity_type="org")
