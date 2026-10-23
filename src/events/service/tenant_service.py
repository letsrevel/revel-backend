"""Tenant resolution: map a custom-domain hostname to its organization (white-label, #1152).

The mapping reads ``Organization.custom_domain``, the denormalized hostname of the organization's
ACTIVE domain, and is cached per hostname (positive answers briefly, negative ones even more
briefly). ``events.service.custom_domain_service`` invalidates the entry on every status change.

Cache ops fail open: a broken or unreachable cache degrades to the plain DB lookup (a failed read
counts as a miss, a failed write or invalidation is skipped with a warning) and never fails a request
or a domain transition. A skipped invalidation is bounded by the TTLs.

Everything here honours the platform kill switch ``Organization.custom_domain_enabled``: with it off,
the organization's hostname no longer resolves.
"""

import re
from urllib.parse import urlsplit
from uuid import UUID

import structlog
from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, OuterRef
from django.http import HttpRequest

from events.models import AdditionalResource, MembershipSubscriptionPlan, Organization, ResourceVisibility
from events.utils.hostname import MAX_HOSTNAME_LENGTH, normalize_hostname

logger = structlog.get_logger(__name__)

POSITIVE_TTL = 60
NEGATIVE_TTL = 15

_CACHE_KEY = "tenant:host:v1:{host}"
_NO_TENANT = ""  # cached sentinel for "no organization serves this hostname"
_HOST_RE = re.compile(r"^[a-z0-9.-]+$")


def _cache_key(host: str) -> str:
    return _CACHE_KEY.format(host=host)


def _clean_host(hostname: str) -> str | None:
    """Normalize a lookup hostname, or return ``None`` when it can never be a tenant host.

    This only filters malformed input (wrong characters, overlong); it does not bound the key space,
    since any well-formed hostname still gets a cache entry. What limits that is the short negative TTL:
    entries for hostnames nobody serves expire after ``NEGATIVE_TTL`` seconds.
    """
    if len(hostname) > MAX_HOSTNAME_LENGTH + 1:  # +1: a trailing dot; checked before the (costlier) IDNA step
        return None
    host = normalize_hostname(hostname)
    if not host or len(host) > MAX_HOSTNAME_LENGTH or not _HOST_RE.match(host):
        return None
    return host


def _tenant_organization_id(hostname: str) -> UUID | None:
    """Return the id of the organization whose active domain is ``hostname`` (cached)."""
    host = _clean_host(hostname)
    if host is None:
        return None
    key = _cache_key(host)
    try:
        cached = cache.get(key)
    except Exception:
        logger.warning("tenant_cache_get_failed", exc_info=True)
        cached = None
    if cached is not None:
        return UUID(cached) if cached != _NO_TENANT else None
    org_id = (
        Organization.objects.filter(custom_domain=host, custom_domain_enabled=True).values_list("id", flat=True).first()
    )
    # ponytail: a reader that queried the DB before a commit can re-cache the old answer after the
    # invalidation ran. The staleness is bounded by the TTL (<= POSITIVE_TTL / NEGATIVE_TTL). Upgrade
    # path if that ever matters: versioned keys (bump a per-host version on invalidation).
    try:
        cache.set(key, str(org_id) if org_id else _NO_TENANT, POSITIVE_TTL if org_id else NEGATIVE_TTL)
    except Exception:
        logger.warning("tenant_cache_set_failed", exc_info=True)
    return org_id


def get_tenant_organization(hostname: str) -> Organization | None:
    """Return the organization serving ``hostname`` as its active custom domain, if any.

    Args:
        hostname: A bare hostname (no port); case and a trailing dot are normalized.

    Returns:
        Organization | None: The organization annotated with ``has_membership_plans`` and
        ``has_resources``, or ``None`` for unknown, pending, failed or removed domains, and for
        organizations whose ``custom_domain_enabled`` kill switch is off.
    """
    org_id = _tenant_organization_id(hostname)
    if org_id is None:
        return None
    return (
        Organization.objects.annotate(
            has_membership_plans=Exists(
                MembershipSubscriptionPlan.objects.filter(tier__organization=OuterRef("pk"), is_active=True)
            ),
            # Publicly visible resources only: the answer is the same for every visitor of the domain.
            has_resources=Exists(
                AdditionalResource.objects.filter(
                    organization=OuterRef("pk"),
                    display_on_organization_page=True,
                    visibility__in=ResourceVisibility.publicly_accessible(),
                )
            ),
        )
        .filter(id=org_id)
        .first()
    )


def is_tenant_origin(origin_host: str) -> bool:
    """Whether ``origin_host`` is the active custom domain of some organization.

    ``False`` when that organization's ``custom_domain_enabled`` kill switch is off. Also what
    Caddy's on-demand TLS ask endpoint (``events.controllers.tls_ask``) decides with.

    Args:
        origin_host: The host part of a request ``Origin`` (port already stripped).

    Returns:
        bool: ``True`` when an organization serves it.
    """
    return _tenant_organization_id(origin_host) is not None


def hostname_from_origin(origin: str) -> tuple[str, str] | None:
    """Split an ``Origin`` header value into ``(scheme, hostname)``; ``None`` when unparsable."""
    try:
        parts = urlsplit(origin)
        hostname = parts.hostname
    except ValueError:
        return None
    if not parts.scheme or not hostname:
        return None
    return parts.scheme, hostname


def tenant_host_from_origin(origin: str | None) -> str | None:
    """Return the normalized hostname of ``origin`` when it is an active tenant origin, else ``None``.

    The single place that decides whether an ``Origin`` header value names a tenant domain: the
    dynamic CORS receiver and :func:`tenant_return_host` both use it. The scheme must be ``https``
    (``http`` only when ``DEBUG``), the host must be the active custom domain of an organization,
    and that organization's ``custom_domain_enabled`` kill switch must be on. Cache failures fail
    open to the DB lookup (see the module docstring), so this never raises on a broken cache.

    Args:
        origin: A raw ``Origin`` header value, or ``None`` when the header is absent.

    Returns:
        str | None: The lowercase hostname (port and trailing dot stripped), or ``None``.
    """
    if not origin or (parsed := hostname_from_origin(origin)) is None:
        return None
    scheme, hostname = parsed
    if scheme != "https" and not (settings.DEBUG and scheme == "http"):
        return None
    if (host := _clean_host(hostname)) is None or not is_tenant_origin(host):
        return None
    return host


def tenant_return_host(request: HttpRequest) -> str | None:
    """Return the tenant hostname the user is coming from, to be carried as a ``return_host`` hint.

    SECURITY: this is a *return hint only*. Account-security token links (email verification,
    password reset, email-change and account-deletion confirmation, unsubscribe) must keep using
    the platform base URL, never a tenant domain, because the organization owner controls that
    domain's DNS and could otherwise harvest platform-wide tokens. The hint only lets the platform
    page send the user back to the tenant afterwards, with no token in that redirect; the frontend
    re-validates it before use.

    Args:
        request: The incoming request; only its ``Origin`` header is read.

    Returns:
        str | None: The hostname of an active tenant domain (kill switch on) named by ``Origin``,
        or ``None`` (no header, platform or unknown host, pending/failed domain, bad scheme).
    """
    return tenant_host_from_origin(request.headers.get("origin"))


def invalidate_tenant_cache(hostname: str) -> None:
    """Drop the cached resolution for ``hostname`` (called on every domain status change)."""
    if host := _clean_host(hostname):
        try:
            cache.delete(_cache_key(host))
        except Exception:
            logger.warning("tenant_cache_delete_failed", exc_info=True)
