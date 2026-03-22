"""User billing profile management endpoints."""

from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from accounts.models import UserBillingProfile
from accounts.schema import (
    UserBillingProfileCreateSchema,
    UserBillingProfileSchema,
    UserBillingProfileUpdateSchema,
    UserVATIdUpdateSchema,
)
from accounts.service import billing_profile_service
from common.authentication import I18nJWTAuth
from common.controllers.base import UserAwareController
from common.throttling import UserDefaultThrottle, WriteThrottle


@api_controller(
    "/me/billing",
    auth=I18nJWTAuth(),
    tags=["User Billing"],
    throttle=UserDefaultThrottle(),
)
class UserBillingController(UserAwareController):
    """CRUD endpoints for user billing profile."""

    @route.get("", url_name="get_billing_profile", response=UserBillingProfileSchema)
    def get_billing_profile(self) -> UserBillingProfile:
        """Get the authenticated user's billing profile."""
        return get_object_or_404(UserBillingProfile, user=self.user())

    @route.post(
        "", url_name="create_billing_profile", response={201: UserBillingProfileSchema}, throttle=WriteThrottle()
    )
    def create_billing_profile(self, payload: UserBillingProfileCreateSchema) -> tuple[int, UserBillingProfile]:
        """Create a billing profile for the authenticated user."""
        return 201, billing_profile_service.create_billing_profile(self.user(), payload.model_dump())

    @route.put("", url_name="update_billing_profile", response=UserBillingProfileSchema, throttle=WriteThrottle())
    def update_billing_profile(self, payload: UserBillingProfileUpdateSchema) -> UserBillingProfile:
        """Replace the billing info on the authenticated user's profile."""
        profile = get_object_or_404(UserBillingProfile, user=self.user())
        billing_profile_service.update_billing_info(profile, payload.model_dump())
        return profile

    @route.put("/vat-id", url_name="set_billing_vat_id", response=UserBillingProfileSchema, throttle=WriteThrottle())
    def set_vat_id(self, payload: UserVATIdUpdateSchema) -> UserBillingProfile:
        """Set or update the user's VAT ID with VIES validation."""
        profile = get_object_or_404(UserBillingProfile, user=self.user())
        billing_profile_service.set_user_vat_id(profile, payload.vat_id)
        return profile

    @route.delete("/vat-id", url_name="delete_billing_vat_id", response={204: None}, throttle=WriteThrottle())
    def delete_vat_id(self) -> tuple[int, None]:
        """Clear the user's VAT ID and validation status."""
        profile = get_object_or_404(UserBillingProfile, user=self.user())
        billing_profile_service.clear_vat_fields(profile)
        return 204, None

    @route.delete("", url_name="delete_billing_profile", response={204: None}, throttle=WriteThrottle())
    def delete_billing_profile(self) -> tuple[int, None]:
        """Delete the user's entire billing profile."""
        get_object_or_404(UserBillingProfile, user=self.user()).delete()
        return 204, None
