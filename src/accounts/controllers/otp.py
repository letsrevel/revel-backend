"""This module contains the controllers for the authentication app."""

import pyotp
import structlog
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import (
    api_controller,
    route,
    status,
)

from accounts import schema
from accounts.models import RevelUser
from accounts.service import totp as totp_service
from common.authentication import I18nJWTAuth
from common.controllers.base import UserAwareController
from common.throttling import OTPThrottle
from common.types import HttpRequest

logger = structlog.get_logger(__name__)


# A *user* throttle: an anonymous-only throttle skips authenticated requests, which would leave
# TOTP verification unlimited.
@api_controller("/otp", tags=["OTP"], auth=I18nJWTAuth(), throttle=OTPThrottle())
class OtpController(UserAwareController):
    @route.get(
        "/setup",
        response=schema.TOTPProvisioningUriSchema,
        url_name="setup-otp",
    )
    def setup_otp(
        self,
    ) -> schema.TOTPProvisioningUriSchema:
        """Get the TOTP provisioning URI to configure an authenticator app.

        Returns a URI (often as QR code) to scan with authenticator apps like Google Authenticator
        or Authy. Returns 400 if 2FA is already enabled. After scanning, verify the setup with
        POST /otp/verify to activate 2FA.
        """
        user = self.user()
        if user.totp_active:
            raise HttpError(400, str(_("OTP is already enabled.")))
        provisioning_uri = pyotp.totp.TOTP(user.totp_secret).provisioning_uri(
            name=user.email,
            issuer_name=settings.TOTP_ISSUER_NAME,
        )
        return schema.TOTPProvisioningUriSchema(uri=provisioning_uri)

    @route.post(
        "/verify",
        response={200: schema.RevelUserSchema, 429: schema.LoginRateLimitSchema},
        url_name="enable-otp",
    )
    def enable_otp(self, payload: schema.OTPVerifySchema) -> RevelUser:
        """Activate 2FA by verifying the TOTP code from the authenticator app.

        Call this after GET /otp/setup with a code from your authenticator app to confirm
        it's configured correctly. On success, activates 2FA for the account. Future logins
        will require the TOTP code via POST /auth/token/pair/otp. Returns 403 if code is invalid, and 429
        (code `totp_locked`, with a `Retry-After` header) after too many wrong codes.
        """
        user = self.user()
        if not totp_service.verify_totp(user, payload.otp, reason="enable"):
            raise HttpError(status.HTTP_403_FORBIDDEN, str(_("Invalid OTP")))
        user.totp_active = True
        user.save(update_fields=["totp_active"])  # only this field: the row can have changed since the sign-in
        return user

    @route.post(
        "/disable",
        response={200: schema.RevelUserSchema, 429: schema.LoginRateLimitSchema},
        url_name="disable-otp",
    )
    def disable_otp(self, request: HttpRequest, payload: schema.OTPVerifySchema) -> RevelUser:
        """Deactivate 2FA after verifying the current TOTP code.

        Requires the current TOTP code to prevent unauthorized disabling. After disabling,
        login will only require email and password via POST /auth/token/pair. Returns 403
        if the TOTP code is invalid, and 429 (code `totp_locked`, with a `Retry-After` header)
        after too many wrong codes.
        """
        user = self.user()
        if not totp_service.verify_totp(user, payload.otp, reason="disable"):
            raise HttpError(status.HTTP_403_FORBIDDEN, str(_("Invalid OTP")))
        user.totp_active = False
        user.save(update_fields=["totp_active"])  # only this field: the row can have changed since the sign-in
        return user
