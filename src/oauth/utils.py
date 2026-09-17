"""Dependency-free helpers safe to import from ``common`` and settings-adjacent code."""

from django.conf import settings


def oauth_provider_enabled() -> bool:
    """Whether the OAuth/OIDC provider is switched on (signing key configured).

    Read at call time so tests can override the setting.

    Returns:
        True when ``OIDC_SIGNING_KEY_PATH`` is set to a non-blank value.
    """
    return bool(str(getattr(settings, "OIDC_SIGNING_KEY_PATH", "")).strip())
