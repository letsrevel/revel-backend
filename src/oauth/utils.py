"""Dependency-free helpers safe to import from ``common`` and settings-adjacent code."""

from django.conf import settings


def oauth_provider_enabled() -> bool:
    """Whether the OAuth/OIDC provider is switched on (signing key configured and readable).

    Read at call time so tests can override the setting.

    Returns:
        True when ``OIDC_SIGNING_KEY_PATH`` is set to a non-blank value and no configured key
        failed to load at settings import (``OIDC_SIGNING_KEY_ERRORS``, reported as
        ``oauth.E002``).
    """
    if getattr(settings, "OIDC_SIGNING_KEY_ERRORS", []):
        return False
    return bool(str(getattr(settings, "OIDC_SIGNING_KEY_PATH", "")).strip())
