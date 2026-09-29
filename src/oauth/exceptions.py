"""Exceptions raised by the oauth app; rendered by ``oauth.exception_handlers``."""


class OAuthProviderDisabledError(Exception):
    """The provider is switched off (no signing key)."""


class AuthorizationRequestError(Exception):
    """An invalid authorization request; ``error`` is the RFC 6749 code."""

    def __init__(self, error: str, description: str) -> None:
        """Initialise with the RFC 6749 error code and its human-readable description."""
        super().__init__(description)
        self.error = error
        self.description = description


class InsufficientScopeError(Exception):
    """The app token lacks the scope this route requires (RFC 6750 §3.1).

    ``scope`` is optional: a permission key that maps to no scope has nothing to
    name in the challenge, and a ``PermissionKey`` must never be leaked in its
    place (spec §7.1).
    """

    def __init__(self, scope: str | None = None) -> None:
        """Initialise with the missing scope name, or nothing when there is no scope to name."""
        super().__init__(scope or "")
        self.scope = scope


class AppLimitReachedError(Exception):
    """The user already owns ``OAUTH_MAX_APPS_PER_USER`` apps."""
