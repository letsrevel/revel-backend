"""User billing profile VIES validation service.

Thin wrapper around the generic ``validate_and_update_vat_entity`` in common.
"""

import typing as t

from common.service.vies_service import VIESValidationResult, validate_and_update_vat_entity

if t.TYPE_CHECKING:
    from accounts.models import UserBillingProfile


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
    return validate_and_update_vat_entity(profile, entity_id=str(profile.user_id), entity_type="user")
