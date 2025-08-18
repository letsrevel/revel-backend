"""This module contains the controllers for the authentication app."""

import typing as t

import structlog
from django.conf import settings
from django_google_sso.models import GoogleSSOUser
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_jwt.controller import TokenObtainPairController
from ninja_jwt.schema import TokenObtainPairInputSchema, TokenObtainPairOutputSchema
from ninja_jwt.tokens import RefreshToken

from accounts import schema
from accounts.service import auth as auth_service
from common.throttling import AuthThrottle

from ..models import RevelUser

logger = structlog.get_logger(__name__)


@api_controller("/auth", tags=["Auth"], throttle=AuthThrottle())
class AuthController(TokenObtainPairController):
    @route.post("/token/pair", response=TokenObtainPairOutputSchema | schema.TempToken, url_name="token_obtain_pair")
    def obtain_token(self, user_token: TokenObtainPairInputSchema) -> TokenObtainPairOutputSchema | schema.TempToken:
        """Obtain a token pair for the user.

        If the user has TOTP enabled, return a temporary token to be used with the OTP.
        """
        user: RevelUser = t.cast(RevelUser, user_token._user)
        if GoogleSSOUser.objects.filter(user=user).exists():
            raise HttpError(401, "Login via SSO.")
        if user and user.totp_active:
            token = auth_service.get_temporary_otp_jwt(user)
            return schema.TempToken(token=token)
        return t.cast(TokenObtainPairOutputSchema, user_token.to_response_schema())  # type: ignore[no-untyped-call]

    if settings.DEMO_MODE:

        @route.post("/demo/token/pair", response=TokenObtainPairOutputSchema, url_name="demo_token_obtain_pair")
        def demo_obtain_token(self, user_token: schema.DemoLoginSchema) -> TokenObtainPairOutputSchema:
            """Obtain a token pair for a demo user, provided their email ends with @example.com."""
            user, _ = RevelUser.objects.get_or_create(
                username=user_token.username,
                defaults={
                    "email": user_token.username,
                    "is_active": True,
                    "email_verified": True,
                },
            )
            user.set_password(user_token.password)
            user.save()
            token = RefreshToken.for_user(user)
            return TokenObtainPairOutputSchema(
                username=user.username,
                access=str(token.access_token),  # type: ignore[attr-defined]
                refresh=str(token),
            )

    @route.post("/token/pair/otp", response=TokenObtainPairOutputSchema, url_name="token_obtain_pair_otp")
    def obtain_token_with_otp(self, payload: schema.TempTokenWithTOTP) -> TokenObtainPairOutputSchema:
        """Obtain a token pair for the user with OTP.

        Takes as input the temporary token from the endpoint above and the OTP code.
        """
        user, verified = auth_service.verify_otp_jwt(payload.token, payload.otp)
        if not verified:
            raise HttpError(401, "Invalid OTP.")
        return auth_service.get_token_pair_for_user(user)

    @route.post("/google/login", response=TokenObtainPairOutputSchema, url_name="google_sso_login")
    def google_login(self, payload: schema.GoogleIDTokenSchema) -> TokenObtainPairOutputSchema:
        """Log in or register a user using Google SSO."""
        return auth_service.google_login(payload.id_token)
