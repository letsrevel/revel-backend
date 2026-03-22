"""User billing profile management.

Thin wrappers around the generic billing operations in ``common.service.vies_service``.
"""

import typing as t

from django.db.models import Q
from ninja.errors import HttpError

from common.service.vies_service import VIESValidationResult, validate_and_update_vat_entity
from common.service.vies_service import clear_vat_fields as _clear_vat_fields
from common.service.vies_service import set_vat_id as _set_vat_id
from common.service.vies_service import update_billing_info as _update_billing_info
from common.utils import get_or_create_with_race_protection

if t.TYPE_CHECKING:
    from accounts.models import RevelUser, UserBillingProfile


def validate_and_update_billing_profile(profile: "UserBillingProfile") -> VIESValidationResult:
    """Validate a billing profile's VAT ID via VIES and update model fields."""
    return validate_and_update_vat_entity(profile, entity_id=str(profile.user_id), entity_type="user")


def create_billing_profile(user: "RevelUser", data: dict[str, t.Any]) -> "UserBillingProfile":
    """Create a billing profile for a user. Raises HttpError 409 if one already exists."""
    from accounts.models import UserBillingProfile

    if data.get("billing_email") is None:
        data["billing_email"] = ""
    profile, created = get_or_create_with_race_protection(
        UserBillingProfile,
        Q(user=user),
        defaults={"user": user, **data},
    )
    if not created:
        raise HttpError(409, "Billing profile already exists.")
    return profile


def update_billing_info(profile: "UserBillingProfile", data: dict[str, t.Any]) -> None:
    """Update billing info fields. Validates vat_country_code vs VAT ID prefix."""
    _update_billing_info(profile, data)


def set_user_vat_id(profile: "UserBillingProfile", vat_id: str) -> None:
    """Set a VAT ID on a billing profile and validate via VIES.

    Rolls back the VAT ID on invalid VIES result. No background retry on VIES failure.
    """
    _set_vat_id(
        profile,
        vat_id,
        entity_id=str(profile.user_id),
        entity_type="user",
        rollback_on_invalid=True,
    )


def clear_vat_fields(profile: "UserBillingProfile") -> None:
    """Clear all VAT-related fields on a billing profile."""
    _clear_vat_fields(profile)
