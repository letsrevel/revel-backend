"""User billing profile VIES validation service.

Wraps the common VIES validation service with UserBillingProfile model updates.
Same pattern as events.service.vies_service.validate_and_update_organization.
"""

import typing as t

import structlog
from django.utils import timezone

from common.service.vies_service import VIESValidationResult, validate_vat_id

if t.TYPE_CHECKING:
    from accounts.models import UserBillingProfile

logger = structlog.get_logger(__name__)


def validate_and_update_billing_profile(profile: "UserBillingProfile") -> VIESValidationResult:
    """Validate a billing profile's VAT ID and update the model fields.

    On successful validation, auto-fills billing_name, billing_address,
    and vat_country_code from the VIES response if they are currently empty.

    Args:
        profile: UserBillingProfile with a vat_id to validate.

    Returns:
        The VIESValidationResult.

    Raises:
        VIESUnavailableError: If VIES is unavailable.
        ValueError: If the profile has no VAT ID set.
    """
    if not profile.vat_id:
        raise ValueError("Billing profile has no VAT ID to validate.")

    result = validate_vat_id(profile.vat_id)

    update_fields = ["vat_id_validated", "vat_id_validated_at", "vies_request_identifier", "updated_at"]

    profile.vat_id_validated = result.valid
    profile.vat_id_validated_at = timezone.now()
    profile.vies_request_identifier = result.request_identifier

    if result.valid:
        # Always sync country code from VAT ID prefix
        country_from_vat = profile.vat_id[:2].upper()
        if profile.vat_country_code != country_from_vat:
            profile.vat_country_code = country_from_vat
            update_fields.append("vat_country_code")

        # Auto-fill billing name from VIES response if empty
        if not profile.billing_name and result.name:
            name = result.name.strip()
            if name and name != "---":
                profile.billing_name = name
                update_fields.append("billing_name")

        # Auto-fill billing address from VIES response if empty
        if not profile.billing_address and result.address:
            address = result.address.strip()
            if address and address != "---":
                profile.billing_address = address
                update_fields.append("billing_address")

        logger.info("user_vat_id_validated", vat_id=profile.vat_id, user_id=str(profile.user_id))
    else:
        logger.warning("user_vat_id_invalid", vat_id=profile.vat_id, user_id=str(profile.user_id))

    profile.save(update_fields=update_fields)

    return result
