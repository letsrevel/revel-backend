"""Deploy-time validation of the provider's settings (spec §5).

Registered from ``OauthConfig.ready()``, so a misconfigured instance fails ``manage.py check``
(and therefore ``migrate``/``runserver``) instead of quietly serving broken discovery.
"""

import typing as t

from django.conf import settings
from django.core.checks import CheckMessage, Error, register

from oauth.utils import oauth_provider_enabled

ISSUER_CHECK_ID = "oauth.E001"


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
