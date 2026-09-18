"""Route-level gates for the provider: the feature flag, and scopes for unkeyed routes."""

from django.http import HttpRequest
from ninja_extra import ControllerBase
from ninja_extra.permissions import BasePermission

from common.authentication import OAuthPrincipal
from oauth.exceptions import InsufficientScopeError, OAuthProviderDisabledError
from oauth.utils import oauth_provider_enabled


class RequireScope(BasePermission):
    """Pass session principals; require ``scope`` for app tokens."""

    def __init__(self, scope: str) -> None:
        """Store the scope this route requires of an app token.

        Args:
            scope: A member of :data:`oauth.scopes.SCOPES`.

        Raises:
            ValueError: ``scope`` is not a known scope. The comparison below is a plain
                ``not in``, so a typo would silently deny every app token on this route
                with no signal; failing at import time is the only way to notice (R-51).
        """
        from oauth.scopes import SCOPES

        if scope not in SCOPES:
            raise ValueError(f"Unknown scope {scope!r}; expected one of {sorted(SCOPES)}.")
        self.scope = scope

    def has_permission(self, request: HttpRequest, controller: ControllerBase) -> bool:
        """Return True, or raise ``InsufficientScopeError`` for an app token without the scope."""
        principal = getattr(request, "auth", None)
        if isinstance(principal, OAuthPrincipal) and self.scope not in principal.scopes:
            raise InsufficientScopeError(self.scope)
        return True

    def has_object_permission(self, request: HttpRequest, controller: ControllerBase, obj: object) -> bool:
        """Object-level check; the scope requirement does not depend on the object."""
        return self.has_permission(request, controller)


class ProviderEnabled(BasePermission):
    """Make every route it guards indistinguishable from a route that does not exist.

    The provider is enabled iff a signing key is configured (ADR-0008), and issue #986's
    acceptance criterion is that with it unset *every* provider route answers 404. The
    protocol views get that from the gating wrappers in ``oauth/urls.py``; the ninja
    controllers are registered unconditionally (the flag is read at call time so tests can
    flip it), so they need this (R-124). Without it the developer portal was half-live with
    the provider off: the reads answered ``200 []`` and delete/activate/rotate-secret worked,
    while create and update 400'd out of DOT's own RS256 validation.

    ``OAuthProviderDisabledError`` is rendered as a static 404 by
    ``oauth.exception_handlers``, so nothing distinguishes it from a missing route.
    """

    def has_permission(self, request: HttpRequest, controller: ControllerBase) -> bool:
        """Return True, or raise ``OAuthProviderDisabledError`` while the provider is off.

        Raises:
            OAuthProviderDisabledError: No signing key is configured; rendered as a 404.
        """
        if not oauth_provider_enabled():
            raise OAuthProviderDisabledError()
        return True

    def has_object_permission(self, request: HttpRequest, controller: ControllerBase, obj: object) -> bool:
        """Object-level check; the feature flag does not depend on the object."""
        return self.has_permission(request, controller)
