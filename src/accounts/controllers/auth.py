"""This module contains the controllers for the authentication app."""

import typing as t

import structlog
from django.utils.translation import gettext_lazy as _
from django_google_sso.models import GoogleSSOUser
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_jwt.controller import TokenObtainPairController
from ninja_jwt.schema import TokenObtainPairInputSchema, TokenObtainPairOutputSchema

from accounts import schema
from accounts.service import auth as auth_service
from accounts.service import impersonation as impersonation_service
from common.throttling import AuthThrottle

from ..models import RevelUser

logger = structlog.get_logger(__name__)


@api_controller("/auth", tags=["Auth"], throttle=AuthThrottle())
class AuthController(TokenObtainPairController):
    @route.post("/token/pair", response=TokenObtainPairOutputSchema | schema.TempToken, url_name="token_obtain_pair")
    def obtain_token(self, user_token: TokenObtainPairInputSchema) -> TokenObtainPairOutputSchema | schema.TempToken:
        """Authenticate with email and password to obtain JWT access/refresh tokens.

        For users without 2FA: Returns standard JWT token pair for immediate access.
        For users with TOTP enabled: Returns a temporary token that must be exchanged for
        a full token pair via POST /auth/token/pair/otp along with the TOTP code.
        Users registered via Google SSO must use POST /auth/google/login instead.
        """
        user: RevelUser = t.cast(RevelUser, user_token._user)
        if GoogleSSOUser.objects.filter(user=user).exists():
            raise HttpError(401, str(_("Login via SSO.")))
        if user and user.totp_active:
            token = auth_service.get_temporary_otp_jwt(user)
            return schema.TempToken(token=token)
        return t.cast(TokenObtainPairOutputSchema, user_token.to_response_schema())  # type: ignore[no-untyped-call]

    @route.post("/token/pair/otp", response=TokenObtainPairOutputSchema, url_name="token_obtain_pair_otp")
    def obtain_token_with_otp(self, payload: schema.TempTokenWithTOTP) -> TokenObtainPairOutputSchema:
        """Complete 2FA authentication by exchanging temporary token and TOTP code for JWT tokens.

        Call this after POST /auth/token/pair returns a temporary token for a 2FA-enabled user.
        Validates the TOTP code from the user's authenticator app and returns a standard JWT token
        pair on success. Returns 401 if the TOTP code is invalid.
        """
        user, verified = auth_service.verify_otp_jwt(payload.token, payload.otp)
        if not verified:
            raise HttpError(401, str(_("Invalid OTP.")))
        return auth_service.get_token_pair_for_user(user)

    @route.post("/google/login", response=TokenObtainPairOutputSchema, url_name="google_sso_login")
    def google_login(self, payload: schema.GoogleIDTokenSchema) -> TokenObtainPairOutputSchema:
        """Authenticate or register via Google SSO using a Google ID token.

        Verifies the Google ID token, creates a new user if needed, and returns JWT tokens.
        For existing Google SSO users, this is the only valid login method - they cannot
        use password-based authentication.
        """
        return auth_service.google_login(payload.id_token)

    @route.post("/impersonate", response=schema.ImpersonationTokenResponseSchema, url_name="impersonate")
    def redeem_impersonation_token(
        self, payload: schema.ImpersonationTokenRequestSchema
    ) -> schema.ImpersonationTokenResponseSchema:
        """Exchange an impersonation request token for an access token.

        This endpoint is called by the frontend after being redirected from the admin panel
        with an impersonation request token. No authentication is required since the token
        itself serves as authentication.

        The returned access token is valid for 15 minutes and includes claims identifying
        this as an impersonated session. No refresh token is issued - the admin must
        initiate a new impersonation session if needed.

        Returns 401 if the token is invalid, expired, or already used.
        Returns 403 if impersonation is no longer allowed (e.g., target became staff).
        """
        result = impersonation_service.redeem_impersonation_token(payload.token)
        return schema.ImpersonationTokenResponseSchema(
            access_token=result.access_token,
            expires_in=result.expires_in,
            user=schema.ImpersonatedUserSchema(
                id=result.user.id,
                email=result.user.email,
                display_name=result.user.display_name,
                first_name=result.user.first_name,
                last_name=result.user.last_name,
            ),
            impersonated_by=result.admin_email,
        )
