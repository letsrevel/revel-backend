"""Org-aware frontend base URL (white-label, #1153).

Light helper (no service imports; it only reads the cached ``SiteSettings`` singleton) so
models, services, signals and notification rendering can all use it. Attendee-facing
links of an organization with an active custom domain are canonical on that domain (the
frontend 301s platform URLs there). Org-admin links, account emails, unsubscribe links
and Stripe Connect onboarding stay on the platform and must keep using
``SiteSettings.frontend_base_url`` directly.

Two more things deliberately stay on the platform origin, because the frontend's
canonical 301 (which preserves the query string) forwards the buyer or user anyway:

- **Stripe return URLs** (ticket, series-pass and membership checkout success/cancel, the
  billing-portal default). Stripe must receive nothing organizer-authored or
  organizer-conditional (#848), and the URL must stay byte-identical across retries of an
  idempotent session creation (``idempotency_key=reservation_id``), which a URL derived
  from mutable domain state would break.
- **Telegram keyboards**.
"""

import typing as t

from django.conf import settings

if t.TYPE_CHECKING:
    from events.models import Organization


def frontend_base_url_for_domain(custom_domain: str | None, custom_domain_enabled: bool) -> str:
    """Return the frontend origin for an organization's custom-domain state.

    Same rule as :func:`frontend_base_url_for`, for callers holding the two columns
    (e.g. from a cache) instead of an ``Organization`` instance.

    Args:
        custom_domain: The organization's active custom hostname, if any.
        custom_domain_enabled: The platform kill switch for that organization.

    Returns:
        str: The origin without a trailing slash.
    """
    from common.models import SiteSettings

    if custom_domain and custom_domain_enabled:
        return str(settings.CUSTOM_DOMAIN_URL_TEMPLATE).format(hostname=custom_domain).rstrip("/")
    return str(SiteSettings.get_solo().frontend_base_url).rstrip("/")


def frontend_base_url_for(org: "Organization | None") -> str:
    """Return the frontend origin to use for an organization's attendee-facing links.

    The tenant origin (``settings.CUSTOM_DOMAIN_URL_TEMPLATE`` formatted with the
    hostname) when the organization has an active custom domain and the platform kill
    switch ``custom_domain_enabled`` is on; otherwise the platform
    ``SiteSettings.frontend_base_url``. Never issues a query beyond the (cached)
    ``SiteSettings`` singleton, so pass an organization the caller already holds.

    Not for Stripe return URLs or Telegram keyboards, which stay on the platform (see the
    module docstring).

    Args:
        org: The organization the link belongs to, or None for platform-level links.

    Returns:
        str: The origin without a trailing slash.
    """
    if org is None:
        return frontend_base_url_for_domain(None, False)
    return frontend_base_url_for_domain(org.custom_domain, org.custom_domain_enabled)
