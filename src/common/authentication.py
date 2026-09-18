import functools
import typing as t
from dataclasses import dataclass

import structlog
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.http import HttpRequest
from django.utils import translation
from django.utils.translation import gettext_lazy as _
from ninja_extra import status
from ninja_extra.exceptions import APIException
from ninja_jwt.exceptions import InvalidToken

from .auth_base import BaseJWTAuth, PermissionDenied

logger = structlog.get_logger(__name__)


class I18nJWTAuth(BaseJWTAuth):
    """JWT authentication that activates user's preferred language.

    This authentication class extends BaseJWTAuth to automatically activate
    the authenticated user's preferred language for the request. This allows
    all API responses, error messages, and emails to be sent in the user's
    chosen language. Also inherits permission checking capabilities from BaseJWTAuth.

    The language is activated immediately after successful JWT validation,
    before the view handler executes, ensuring all translations work correctly.

    Usage:
        @route.get("/endpoint", auth=I18nJWTAuth())
        def my_endpoint(request):
            # User's language is already activated
            return {"message": str(_("Hello!"))}

        # With email verification requirement:
        @route.post("/create-org", auth=I18nJWTAuth(requires_verified_email=True))
        def create_org(request):
            # Only users with verified emails can access this
            ...
    """

    def authenticate(self, request: HttpRequest, token: str) -> t.Any:
        """Authenticate the request and activate user's language preference.

        Args:
            request: The HTTP request object
            token: The JWT token string

        Returns:
            The authenticated user object

        Raises:
            AuthenticationFailed: If authentication fails
            InvalidToken: If the token is invalid
            PermissionDenied: If user doesn't meet required criteria
        """
        user = super().authenticate(request, token)

        # Activate user's preferred language if set
        if user and hasattr(user, "language"):
            user_language = getattr(user, "language", None)
            if user_language:
                translation.activate(user_language)
                request.LANGUAGE_CODE = user_language

        return user


class OptionalAuth(I18nJWTAuth):
    """Optional JWT authentication with i18n support.

    Allows endpoints to work with or without authentication:
    - If JWT token present: Authenticates user and activates their language preference
    - If no JWT token: Sets request.user to AnonymousUser and continues

    This is useful for public endpoints that show different content based on authentication
    status (e.g., public events vs member-only events).

    Usage:
        @api_controller("/events", auth=OptionalAuth())
        class EventController:
            def list_events(self, request):
                # Works for both authenticated and anonymous users
                user = request.user  # Could be RevelUser or AnonymousUser
    """

    def __call__(self, request: HttpRequest) -> t.Any | None:
        """Overrides I18nJWTAuth __call__ to provide optional auth."""
        headers = request.headers
        auth_value = headers.get(self.header)
        if not auth_value:
            request.user = AnonymousUser()
            return request.user
        parts = auth_value.split(" ")

        if parts[0].lower() != self.openapi_scheme:
            if settings.DEBUG:
                logger.error(f"Unexpected auth - '{auth_value}'")
            return None
        token = " ".join(parts[1:])
        return self.authenticate(request, token)


@dataclass(frozen=True)
class OAuthPrincipal:
    """``request.auth`` for a request authenticated with a third-party app token.

    Attributes:
        client_id: The ``OAuthApplication.client_id`` the token was issued to.
        scopes: The scopes granted at issue time (see ``ScopedJWTAuth``).
    """

    client_id: str
    scopes: frozenset[str]


class InvalidBearerToken(APIException):
    """401 for a rejected app token, with the RFC 6750 / RFC 9728 challenge header.

    Rendered by ``oauth.exception_handlers``, which copies ``www_authenticate`` onto
    the response. The body stays deliberately generic: a client cannot tell an unknown
    token from an expired one, a deactivated app or a deactivated user.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = _("Invalid or expired token.")

    def __init__(self) -> None:
        """Build the challenge from the configured issuer."""
        super().__init__(str(self.default_detail))
        self.www_authenticate = 'Bearer error="invalid_token"'
        # RFC 9728 §5.1: the parameter must be an absolute URI, so it is omitted rather
        # than emitted relative when no issuer is configured.
        if settings.OAUTH_ISSUER:
            self.www_authenticate += (
                f', resource_metadata="{settings.OAUTH_ISSUER}/.well-known/oauth-protected-resource"'
            )


# An app's ``last_used_at`` powers the Connected Apps screen, not billing, so it is
# written at most once per window rather than on every request.
_LAST_USED_BUMP_SECONDS = 60


class ScopedJWTAuth(I18nJWTAuth):
    """Session JWT first; on failure hand the bearer to django-oauth-toolkit (spec §7.2).

    Controllers opt in by using this class. Every controller left on ``I18nJWTAuth`` or
    ``OptionalAuth`` therefore refuses app tokens by construction, which is the whole
    safety argument: reaching a new surface with an app token requires an explicit,
    reviewable change of auth class.
    """

    def authenticate(self, request: HttpRequest, token: str) -> t.Any:
        """Authenticate a session JWT, else a DOT app token.

        Args:
            request: The HTTP request object.
            token: The bearer token string.

        Returns:
            The authenticated user for a session JWT, or an ``OAuthPrincipal`` for an
            app token.

        Raises:
            InvalidToken: The bearer is a failed session JWT (shape-wise or because the
                provider is switched off), re-raised untouched.
            InvalidBearerToken: The bearer reached DOT and was refused.
            PermissionDenied: The route requires a verified email and the user has none.
        """
        try:
            return super().authenticate(request, token)
        except InvalidToken:
            # Shape heuristic: a session JWT is three base64url segments joined by dots,
            # while a DOT access token is 30 characters from oauthlib's
            # UNICODE_ASCII_CHARACTER_SET (letters and digits only, verified against
            # oauthlib 3.x ``generate_token``) and so can never contain a dot. Keep this
            # in sync if ACCESS_TOKEN_GENERATOR is ever set to a dotted format.
            # Consequence, intended: an *expired* session JWT fails here without ever
            # reaching DOT, while a malformed non-JWT bearer falls through to DOT.
            if token.count(".") == 2:
                raise
            from oauth.utils import oauth_provider_enabled

            if not oauth_provider_enabled():
                raise
        return self._authenticate_app_token(request)

    def _authenticate_app_token(self, request: HttpRequest) -> OAuthPrincipal:
        """Validate the bearer as a DOT access token and build the principal.

        No scope checking happens here — that is the permission layer's job. DOT is
        asked for "any valid token" and the route's scope requirement is enforced by
        ``RequireScope`` / the permission classes.

        Args:
            request: The HTTP request carrying the bearer header.

        Returns:
            The principal for the validated token.

        Raises:
            InvalidBearerToken: The token is unknown, expired, userless, issued for a
                deactivated app or user, a DCR registration token, or the route demands
                staff/superuser (which an app token never is).
            PermissionDenied: The route requires a verified email and the user has none.
        """
        from django.utils import timezone
        from oauth2_provider.settings import oauth2_settings

        # ``scopes=[]`` means "any valid token": DOT's ``allow_scopes`` short-circuits to
        # True on an empty required list, so this asserts token validity only.
        valid, oauth_request = _oauthlib_core().verify_request(request, scopes=[])
        if not valid:
            raise InvalidBearerToken()
        access_token = oauth_request.access_token
        user = access_token.user
        # ``access_token.scope.split()``, NOT ``access_token.scopes``: that property filters
        # the stored string through the scopes backend, and ``RegistryScopes`` does not know
        # DOT's DCR registration scope — so the guard below would be dead code. Using the raw
        # string also means the principal reflects what was granted at issue time, not the
        # app's current ``allowed_scopes``; shrinking those revokes the live tokens instead.
        scopes = frozenset(access_token.scope.split())
        # ``application`` is a nullable FK and DOT's own validator explicitly tolerates a null
        # one, but an app-less token is by definition not a third-party app credential — and
        # there would be no ``client_id`` to build a principal from. Refuse rather than crash.
        app = access_token.application
        if user is None or not user.is_active or app is None or oauth2_settings.DCR_REGISTRATION_SCOPE in scopes:
            raise InvalidBearerToken()
        # An app token is never a staff credential (spec §7.2). Deliberately a 401, not
        # the 403 the session path returns: the token itself is unusable here.
        if self.is_staff or self.is_superuser:
            raise InvalidBearerToken()
        if self.requires_verified_email and not user.email_verified:
            raise PermissionDenied(str(_("Email verification required.")))

        request.user = user
        if user.language:
            translation.activate(user.language)
            request.LANGUAGE_CODE = user.language
        if settings.FEATURE_OBSERVABILITY:
            structlog.contextvars.bind_contextvars(user_id=str(user.pk), oauth_client_id=app.client_id)
        now = timezone.now()
        if app.last_used_at is None or (now - app.last_used_at).total_seconds() > _LAST_USED_BUMP_SECONDS:
            type(app).objects.filter(pk=app.pk).update(last_used_at=now)
        return OAuthPrincipal(client_id=app.client_id, scopes=scopes)


@functools.lru_cache(maxsize=1)
def _oauthlib_core() -> t.Any:
    """Return the process-wide ``OAuthLibCore``.

    ``get_oauthlib_core()`` builds a validator and an oauthlib ``Server`` on every call,
    and both are stateless once constructed, so the instance is cached for the process.
    """
    from oauth2_provider.oauth2_backends import get_oauthlib_core

    return get_oauthlib_core()


@receiver(setting_changed)
def _reset_oauthlib_core(**kwargs: t.Any) -> None:
    """Drop the cached core when settings change, mirroring DOT's own reload receiver.

    ``setting_changed`` is emitted only by Django's test utilities (``override_settings``
    and pytest-django's ``settings`` fixture), never on a production request path. The
    signal is not filtered by key because the core is built from several settings and a
    rebuild is cheap compared with reasoning about which ones matter.
    """
    _oauthlib_core.cache_clear()
