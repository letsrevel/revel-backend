"""User billing profile management endpoints."""

import structlog
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from accounts.models import UserBillingProfile
from accounts.schema import (
    UserBillingProfileCreateSchema,
    UserBillingProfileSchema,
    UserBillingProfileUpdateSchema,
    UserVATIdUpdateSchema,
)
from accounts.service.billing_profile_service import validate_and_update_billing_profile
from common.authentication import I18nJWTAuth
from common.controllers.base import UserAwareController
from common.service.vies_service import VIESUnavailableError
from common.throttling import UserDefaultThrottle, WriteThrottle
from common.utils import get_or_create_with_race_protection

logger = structlog.get_logger(__name__)


@api_controller(
    "/me/billing",
    auth=I18nJWTAuth(),
    tags=["User Billing"],
    throttle=UserDefaultThrottle(),
)
class UserBillingController(UserAwareController):
    """CRUD endpoints for user billing profile."""

    @route.get(
        "",
        url_name="get_billing_profile",
        response=UserBillingProfileSchema,
    )
    def get_billing_profile(self) -> UserBillingProfile:
        """Get the authenticated user's billing profile."""
        return get_object_or_404(UserBillingProfile, user=self.user())

    @route.post(
        "",
        url_name="create_billing_profile",
        response={201: UserBillingProfileSchema},
        throttle=WriteThrottle(),
    )
    def create_billing_profile(self, payload: UserBillingProfileCreateSchema) -> tuple[int, UserBillingProfile]:
        """Create a billing profile for the authenticated user."""
        user = self.user()
        data = payload.model_dump()
        if data.get("billing_email") is None:
            data["billing_email"] = ""
        profile, created = get_or_create_with_race_protection(
            UserBillingProfile,
            Q(user=user),
            defaults={"user": user, **data},
        )
        if not created:
            raise HttpError(409, str(_("Billing profile already exists.")))
        return 201, profile

    @route.patch(
        "",
        url_name="update_billing_profile",
        response=UserBillingProfileSchema,
        throttle=WriteThrottle(),
    )
    def update_billing_profile(self, payload: UserBillingProfileUpdateSchema) -> UserBillingProfile:
        """Update the authenticated user's billing profile."""
        profile = get_object_or_404(UserBillingProfile, user=self.user())
        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return profile

        # Reject country code changes that conflict with the VAT ID prefix
        new_country = update_data.get("vat_country_code")
        if new_country and profile.vat_id:
            vat_prefix = profile.vat_id[:2].upper()
            if new_country != vat_prefix:
                raise HttpError(
                    422,
                    str(_("Country code must match the VAT ID prefix (%(prefix)s).") % {"prefix": vat_prefix}),
                )

        for field, value in update_data.items():
            setattr(profile, field, value)
        profile.save(update_fields=[*update_data.keys(), "updated_at"])
        return profile

    @route.put(
        "/vat-id",
        url_name="set_billing_vat_id",
        response=UserBillingProfileSchema,
        throttle=WriteThrottle(),
    )
    def set_vat_id(self, payload: UserVATIdUpdateSchema) -> UserBillingProfile:
        """Set or update the user's VAT ID with VIES validation.

        On invalid VIES result the save is rolled back. On VIES unavailability
        the VAT ID is kept so the user can retry later.
        """
        profile = get_object_or_404(UserBillingProfile, user=self.user())

        profile.vat_id = payload.vat_id
        profile.vat_country_code = payload.vat_id[:2].upper()
        profile.vat_id_validated = False
        profile.vat_id_validated_at = None
        profile.vies_request_identifier = ""
        profile.save(
            update_fields=[
                "vat_id",
                "vat_country_code",
                "vat_id_validated",
                "vat_id_validated_at",
                "vies_request_identifier",
                "updated_at",
            ]
        )

        try:
            result = validate_and_update_billing_profile(profile)
            if not result.valid:
                # Rollback: clear the invalid VAT ID and stale validation metadata
                profile.vat_id = ""
                profile.vat_country_code = ""
                profile.vat_id_validated = False
                profile.vat_id_validated_at = None
                profile.vies_request_identifier = ""
                profile.save(
                    update_fields=[
                        "vat_id",
                        "vat_country_code",
                        "vat_id_validated",
                        "vat_id_validated_at",
                        "vies_request_identifier",
                        "updated_at",
                    ]
                )
                raise HttpError(400, str(_("The VAT ID is not valid according to VIES.")))
        except VIESUnavailableError:
            logger.warning("vies_unavailable_user", user_id=str(profile.user_id))
            raise HttpError(
                503,
                str(
                    _(
                        "VIES validation service is temporarily unavailable."
                        " The VAT ID has been saved. Please try again later."
                    )
                ),
            )

        profile.refresh_from_db()
        return profile

    @route.delete(
        "/vat-id",
        url_name="delete_billing_vat_id",
        response={204: None},
        throttle=WriteThrottle(),
    )
    def delete_vat_id(self) -> tuple[int, None]:
        """Clear the user's VAT ID and validation status."""
        profile = get_object_or_404(UserBillingProfile, user=self.user())
        profile.vat_id = ""
        profile.vat_country_code = ""
        profile.vat_id_validated = False
        profile.vat_id_validated_at = None
        profile.vies_request_identifier = ""
        profile.save(
            update_fields=[
                "vat_id",
                "vat_country_code",
                "vat_id_validated",
                "vat_id_validated_at",
                "vies_request_identifier",
                "updated_at",
            ]
        )
        return 204, None

    @route.delete(
        "",
        url_name="delete_billing_profile",
        response={204: None},
        throttle=WriteThrottle(),
    )
    def delete_billing_profile(self) -> tuple[int, None]:
        """Delete the user's entire billing profile."""
        profile = get_object_or_404(UserBillingProfile, user=self.user())
        profile.delete()
        return 204, None
