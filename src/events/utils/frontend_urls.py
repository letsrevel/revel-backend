"""Org-aware frontend base URL (white-label, #1153).

Pure helper (no service imports) so models, services, signals and notification
rendering can all use it. Attendee-facing links of an organization with an active
custom domain are canonical on that domain (the frontend 301s platform URLs there);
org-admin links, account emails, unsubscribe links and Stripe Connect onboarding stay
on the platform and must keep using ``SiteSettings.frontend_base_url`` directly.
"""

import typing as t

from django.conf import settings

if t.TYPE_CHECKING:
    from events.models import Organization


def frontend_base_url_for(org: "Organization | None") -> str:
    """Return the frontend origin to use for an organization's attendee-facing links.

    The tenant origin (``settings.CUSTOM_DOMAIN_URL_TEMPLATE`` formatted with the
    hostname) when the organization has an active custom domain and the platform kill
    switch ``custom_domain_enabled`` is on; otherwise the platform
    ``SiteSettings.frontend_base_url``. Never issues a query beyond the (cached)
    ``SiteSettings`` singleton, so pass an organization the caller already holds.

    Args:
        org: The organization the link belongs to, or None for platform-level links.

    Returns:
        str: The origin without a trailing slash.
    """
    from common.models import SiteSettings

    if org is not None and org.custom_domain and org.custom_domain_enabled:
        return str(settings.CUSTOM_DOMAIN_URL_TEMPLATE).format(hostname=org.custom_domain).rstrip("/")
    return str(SiteSettings.get_solo().frontend_base_url).rstrip("/")
