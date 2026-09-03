"""This module contains the controllers for the authentication app."""

import typing as t

import structlog
from django.conf import settings
from django.http import HttpRequest, HttpResponseRedirect
from django.utils.translation import gettext_lazy as _
from ninja import Path, Query
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_jwt.controller import TokenObtainPairController
from ninja_jwt.schema import TokenObtainPairInputSchema, TokenObtainPairOutputSchema

from accounts import schema
from accounts.exceptions import OIDCLoginError
from accounts.service import auth as auth_service
from accounts.service import impersonation as impersonation_service
from accounts.service import oidc as oidc_service
from common.throttling import AuthThrottle
from revel.oidc_config import KEY_PATTERN

from ..models import RevelUser

logger = structlog.get_logger(__name__)

ProviderKey = t.Annotated[str, Path(..., pattern=KEY_PATTERN)]


@api_controller("/auth", tags=["Auth"], throttle=AuthThrottle())
class AuthController(TokenObtainPairController):
    @route.post("/token/pair", response=TokenObtainPairOutputSchema | schema.TempToken, url_name="token_obtain_pair")
    def obtain_token(self, user_token: TokenObtainPairInputSchema) -> TokenObtainPairOutputSchema | schema.TempToken:
        """Authenticate with email and password to obtain JWT access/refresh tokens.

        For users without 2FA: Returns standard JWT token pair for immediate access.
        For users with TOTP enabled: Returns a temporary token that must be exchanged for
        a full token pair via POST /auth/token/pair/otp along with the TOTP code.
        Users may also sign in through a configured OpenID Connect provider via GET /auth/oidc/{provider}/start.
        """
        user: RevelUser = t.cast(RevelUser, user_token._user)
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

    @route.get("/oidc/{provider}/start", include_in_schema=False, url_name="oidc_start", response=None)
    def oidc_start(
        self, provider: ProviderKey, return_url: t.Annotated[str | None, Query(max_length=2048)] = None
    ) -> HttpResponseRedirect:
        """Begin an OpenID Connect login: redirect the browser to the provider.

        Not part of the OpenAPI schema — the frontend links here, it does not call it. A
        provider failure raises ``OIDCLoginError``, which the accounts exception handler turns
        into a login-page redirect (the browser is mid-navigation, so JSON would be a dead end).
        """
        config = oidc_service.get_provider(provider)
        start = oidc_service.begin_login(config, return_url)
        response = HttpResponseRedirect(start.url)
        response.set_cookie(
            oidc_service.OIDC_STATE_COOKIE,
            start.state,
            max_age=oidc_service.STATE_TTL_SECONDS,
            httponly=True,
            secure=not settings.DEBUG,
            samesite="Lax",
            path=oidc_service.OIDC_STATE_COOKIE_PATH,
        )
        return response

    @route.get("/oidc/{provider}/callback", include_in_schema=False, url_name="oidc_callback", response=None)
    def oidc_callback(
        self,
        request: HttpRequest,
        provider: ProviderKey,
        code: t.Annotated[str | None, Query(max_length=2048)] = None,
        state: t.Annotated[str | None, Query(max_length=256)] = None,
        error: t.Annotated[str | None, Query(max_length=128)] = None,
    ) -> HttpResponseRedirect:
        """Provider redirect target. Answers with a redirect to the frontend.

        Every failure raises ``OIDCLoginError``; see ``oidc_start`` for why that becomes a
        redirect rather than JSON.
        """
        config = oidc_service.get_provider(provider)
        if error:
            logger.info("oidc_login_denied", provider=provider, error=error)
            raise OIDCLoginError("denied")
        if not code or not state:
            raise OIDCLoginError("state")
        if not oidc_service.state_matches_cookie(request.COOKIES.get(oidc_service.OIDC_STATE_COOKIE), state):
            logger.warning("oidc_state_cookie_mismatch", provider=provider)
            raise OIDCLoginError("state")
        token = oidc_service.complete_login(config, code, state)
        response = HttpResponseRedirect(f"{oidc_service.frontend_base_url()}/auth/callback?token={token}")
        response.delete_cookie(oidc_service.OIDC_STATE_COOKIE, path=oidc_service.OIDC_STATE_COOKIE_PATH)
        return response

    @route.post("/oidc/exchange", response=schema.OIDCExchangeResponseSchema, url_name="oidc_exchange")
    def oidc_exchange(self, payload: schema.OIDCExchangeRequestSchema) -> schema.OIDCExchangeResponseSchema:
        """Exchange the one-time login token from the OIDC callback for JWT tokens.

        Single use, valid for ~60 seconds. Returns 401 if the token is invalid, expired,
        or already redeemed.
        """
        pair, return_url = oidc_service.redeem_login_token(payload.token)
        return schema.OIDCExchangeResponseSchema(
            username=pair.username,  # type: ignore[attr-defined]
            access=pair.access,
            refresh=pair.refresh,
            return_url=return_url,
        )

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
