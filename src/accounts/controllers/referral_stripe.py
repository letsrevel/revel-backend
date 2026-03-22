"""Stripe Connect onboarding endpoints for referrers."""

from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from accounts.models import ReferralCode
from common.authentication import I18nJWTAuth
from common.controllers.base import UserAwareController
from common.models import SiteSettings
from common.schema import StripeAccountStatusSchema, StripeOnboardingLinkSchema
from common.service import stripe_connect_service
from common.throttling import UserDefaultThrottle, WriteThrottle


@api_controller("/referral/stripe", auth=I18nJWTAuth(), tags=["Referral"], throttle=UserDefaultThrottle())
class ReferralStripeController(UserAwareController):
    """Stripe Connect onboarding for referrers."""

    def _assert_is_referrer(self) -> ReferralCode:
        """Return the user's ReferralCode or raise 403."""
        try:
            return ReferralCode.objects.get(user=self.user(), is_active=True)
        except ReferralCode.DoesNotExist:
            raise HttpError(403, str(_("Only active referrers can connect a Stripe account.")))

    @route.post(
        "/connect", url_name="referral_stripe_connect", response=StripeOnboardingLinkSchema, throttle=WriteThrottle()
    )
    def connect(self) -> StripeOnboardingLinkSchema:
        """Create (or re-use) a Stripe Express account and return the onboarding URL.

        If the user already has a ``stripe_account_id``, the existing account is
        reused and a fresh onboarding link is generated.
        """
        self._assert_is_referrer()
        user = self.user()

        account_id = user.stripe_account_id or stripe_connect_service.create_connect_account(
            user, user.email, account_type="express"
        )

        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        refresh_url = f"{frontend_base_url}/referral/settings?stripe_refresh=true"
        return_url = f"{frontend_base_url}/referral/settings?stripe_success=true"
        onboarding_url = stripe_connect_service.create_account_link(account_id, refresh_url, return_url)
        return StripeOnboardingLinkSchema(onboarding_url=onboarding_url)

    @route.get("/verify", url_name="referral_stripe_verify", response=StripeAccountStatusSchema)
    def verify(self) -> StripeAccountStatusSchema:
        """Sync the connected Stripe account status from Stripe and return it."""
        self._assert_is_referrer()
        user = self.user()

        stripe_connect_service.sync_account_status(user)
        user.refresh_from_db(fields=["stripe_charges_enabled", "stripe_details_submitted"])

        return StripeAccountStatusSchema(
            is_connected=user.is_stripe_connected,
            charges_enabled=user.stripe_charges_enabled,
            details_submitted=user.stripe_details_submitted,
        )
