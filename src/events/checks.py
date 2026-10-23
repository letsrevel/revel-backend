"""System checks for the events app, registered from ``EventsConfig.ready()``."""

import re
import typing as t

from django.conf import settings
from django.core.checks import CheckMessage, Warning, register

CNAME_TARGET_MISSING_CHECK_ID = "events.W001"
CNAME_TARGET_MALFORMED_CHECK_ID = "events.W002"

# A bare DNS hostname: dot-separated labels of letters, digits and inner hyphens; no scheme, path, port or spaces.
_BARE_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$", re.IGNORECASE)


@register()
def check_custom_domain_cname_target(app_configs: t.Any, **kwargs: t.Any) -> list[CheckMessage]:
    """Warn when organizers get no usable CNAME target to point their DNS at.

    ``events.W001``: Cloudflare provisioning is on but the target is empty. ``events.W002``: the target is set
    but is not a bare hostname (a scheme, path, port or spaces would be shown to organizers as a CNAME value).

    Args:
        app_configs: Django's app filter (unused; the check is global).
        **kwargs: Django's check kwargs (unused).

    Returns:
        The problems found.
    """
    target = settings.CUSTOM_DOMAIN_CNAME_TARGET
    if target and not _BARE_HOSTNAME.fullmatch(target):
        return [
            Warning(
                f"CUSTOM_DOMAIN_CNAME_TARGET {target!r} is not a bare hostname.",
                hint=(
                    "Set it to the hostname only (e.g. customers.example.com): no scheme, path, port or spaces. "
                    "It is shown to organizers as the CNAME value to create."
                ),
                id=CNAME_TARGET_MALFORMED_CHECK_ID,
            )
        ]
    if settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ZONE_ID and not target:
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
