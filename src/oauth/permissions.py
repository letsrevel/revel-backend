"""Explicit scope requirement for routes with no ``PermissionKey`` (spec §7.3.2)."""

from django.http import HttpRequest
from ninja_extra import ControllerBase
from ninja_extra.permissions import BasePermission

from common.authentication import OAuthPrincipal
from oauth.exceptions import InsufficientScopeError


class RequireScope(BasePermission):
    """Pass session principals; require ``scope`` for app tokens."""

    def __init__(self, scope: str) -> None:
        """Store the scope this route requires of an app token."""
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
