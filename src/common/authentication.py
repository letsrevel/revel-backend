import logging
import typing as t

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.utils import translation
from ninja_jwt.authentication import AsyncJWTAuth, JWTAuth

logger = logging.getLogger(__name__)


class I18nJWTAuth(JWTAuth):
    """JWT authentication that activates user's preferred language.

    This authentication class extends JWTAuth to automatically activate
    the authenticated user's preferred language for the request. This allows
    all API responses, error messages, and emails to be sent in the user's
    chosen language.

    The language is activated immediately after successful JWT validation,
    before the view handler executes, ensuring all translations work correctly.

    Usage:
        @route.get("/endpoint", auth=I18nJWTAuth())
        def my_endpoint(request):
            # User's language is already activated
            return {"message": str(_("Hello!"))}
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
        """
        user = super().authenticate(request, token)

        # Activate user's preferred language if set
        if user and hasattr(user, "language"):
            user_language = getattr(user, "language", None)
            if user_language:
                translation.activate(user_language)
                request.LANGUAGE_CODE = user_language

        return user


class AsyncI18nJWTAuth(AsyncJWTAuth):
    """Async JWT authentication that activates user's preferred language.

    This is the async version of I18nJWTAuth for use with async view handlers.
    It provides the same language activation functionality in an async context.

    Usage:
        @route.get("/endpoint", auth=AsyncI18nJWTAuth())
        async def my_async_endpoint(request):
            # User's language is already activated
            return {"message": str(_("Hello!"))}
    """

    async def authenticate(self, request: HttpRequest, token: str) -> t.Any:
        """Authenticate the request asynchronously and activate user's language.

        Args:
            request: The HTTP request object
            token: The JWT token string

        Returns:
            The authenticated user object

        Raises:
            AuthenticationFailed: If authentication fails
            InvalidToken: If the token is invalid
        """
        user = await super().authenticate(request, token)

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
