"""This module contains the controllers for the authentication app."""

import typing as t

import pyotp
import structlog
from django.conf import settings
from ninja.errors import HttpError
from ninja_extra import (
    ControllerBase,
    api_controller,
    route,
    status,
)
from ninja_jwt.authentication import JWTAuth

from accounts import schema
from accounts.models import RevelUser
from common.throttling import AuthThrottle
from common.types import HttpRequest

logger = structlog.get_logger(__name__)


@api_controller("/otp", tags=["OTP"], auth=JWTAuth(), throttle=AuthThrottle())
class OtpController(ControllerBase):
    def user(self) -> RevelUser:
        """Get the user for this request."""
        return t.cast(RevelUser, self.context.request.user)  # type: ignore[union-attr]

    @route.get(
        "/setup",
        response=schema.TOTPProvisioningUriSchema,
        url_name="setup-otp",
    )
    def setup_otp(
        self,
    ) -> schema.TOTPProvisioningUriSchema:
        """Give the provisioning URI to the user to set up TOTP app."""
        user = self.user()
        if user.totp_active:
            raise HttpError(400, "OTP is already enabled.")
        provisioning_uri = pyotp.totp.TOTP(user.totp_secret).provisioning_uri(
            name=user.email,
            issuer_name=settings.TOTP_ISSUER_NAME,
        )
        return schema.TOTPProvisioningUriSchema(uri=provisioning_uri)

    @route.post(
        "/verify",
        response=schema.RevelUserSchema,
        url_name="enable-otp",
    )
    def enable_otp(self, payload: schema.OTPVerifySchema) -> RevelUser:
        """Verify a user's otp to confirm it works."""
        user = self.user()
        totp = pyotp.TOTP(user.totp_secret)
        if not totp.verify(payload.otp):
            raise HttpError(status.HTTP_403_FORBIDDEN, "Invalid OTP")
        user.totp_active = True
        user.save()
        return user

    @route.post(
        "/disable",
        response=schema.RevelUserSchema,
        url_name="disable-otp",
    )
    def disable_otp(self, request: HttpRequest, payload: schema.OTPVerifySchema) -> RevelUser:
        """Disable the user's OTP."""
        user = self.user()
        totp = pyotp.TOTP(user.totp_secret)
        if not totp.verify(payload.otp):
            raise HttpError(status.HTTP_403_FORBIDDEN, "Invalid OTP")
        user.totp_active = False
        user.save()
        return user
