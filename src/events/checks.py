"""System checks for the events app, registered from ``EventsConfig.ready()``."""

import typing as t

from django.conf import settings
from django.core.checks import CheckMessage, Warning, register

CNAME_TARGET_MISSING_CHECK_ID = "events.W001"


@register()
def check_custom_domain_cname_target(app_configs: t.Any, **kwargs: t.Any) -> list[CheckMessage]:
    """Warn when Cloudflare provisioning is on but organizers get no CNAME target to point their DNS at.

    Args:
        app_configs: Django's app filter (unused; the check is global).
        **kwargs: Django's check kwargs (unused).

    Returns:
        The problems found.
    """
    if settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ZONE_ID and not settings.CUSTOM_DOMAIN_CNAME_TARGET:
        return [
            Warning(
                "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID are set, but CUSTOM_DOMAIN_CNAME_TARGET is empty.",
                hint=(
                    "Set CUSTOM_DOMAIN_CNAME_TARGET to the Cloudflare fallback-origin hostname; without it "
                    "organizers are not told which CNAME to create."
                ),
                id=CNAME_TARGET_MISSING_CHECK_ID,
            )
        ]
    return []
