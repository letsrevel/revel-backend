"""Deploy-time validation of the provider's settings (spec §5).

Registered from ``OauthConfig.ready()``, so a misconfigured instance fails ``manage.py check``
(and therefore ``migrate``/``runserver``) instead of quietly serving broken discovery.
"""

import typing as t

from django.conf import settings
from django.core.checks import CheckMessage, Error, register

from oauth.utils import oauth_provider_enabled

ISSUER_CHECK_ID = "oauth.E001"
SIGNING_KEY_CHECK_ID = "oauth.E002"


@register()
def check_oauth_issuer_configured(app_configs: t.Any, **kwargs: t.Any) -> list[CheckMessage]:
    """``OAUTH_ISSUER`` is mandatory once a signing key switches the provider on.

    Without it DOT falls back to request-relative URLs, the 401 challenge omits
    ``resource_metadata`` and the OIDC ``picture`` claim degrades to a relative path.

    Args:
        app_configs: Django's app filter (unused; the check is global).
        **kwargs: Django's check kwargs (unused).

    Returns:
        One error while the provider is enabled with a blank issuer, otherwise nothing.
    """
    if not oauth_provider_enabled() or settings.OAUTH_ISSUER.strip():
        return []
    return [
        Error(
            "OAUTH_ISSUER must be set when OIDC_SIGNING_KEY_PATH is configured.",
            hint=(
                "Set OAUTH_ISSUER to this API's public origin (e.g. https://api.letsrevel.io), "
                "or unset OIDC_SIGNING_KEY_PATH to disable the OAuth provider."
            ),
            id=ISSUER_CHECK_ID,
        )
    ]


@register()
def check_oidc_signing_key_readable(app_configs: t.Any, **kwargs: t.Any) -> list[CheckMessage]:
    """A configured signing key must be readable by the process, or the provider is silently off.

    ``revel.settings.oauth`` records (rather than raises) a key it could not read, so that a
    permissions mistake on the mounted PEM degrades to "provider disabled" instead of crashing
    every process at import. This is where that mistake becomes loud.

    Args:
        app_configs: Django's app filter (unused; the check is global).
        **kwargs: Django's check kwargs (unused).

    Returns:
        One error per unreadable key path, otherwise nothing.
    """
    errors: list[str] = getattr(settings, "OIDC_SIGNING_KEY_ERRORS", [])
    return [
        Error(
            f"OIDC signing key could not be read: {problem}",
            hint=(
                "Check the path, and that the file is readable by the uid the app runs as — in the "
                "Docker image that is uid 997, so a key generated on the host needs `chmod 644` "
                "(or a matching owner). The OAuth provider stays disabled until it can be read."
            ),
            id=SIGNING_KEY_CHECK_ID,
        )
        for problem in errors
    ]
