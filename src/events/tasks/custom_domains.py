"""Celery tasks for custom-domain provisioning (white-label, #1151)."""

import typing as t

import structlog
from celery import shared_task

from events.exceptions import CloudflareAPIError
from events.models import OrganizationDomain
from events.service import cloudflare_custom_hostnames, custom_domain_service

logger = structlog.get_logger(__name__)


class SyncCustomDomainsResult(t.TypedDict):
    """Counters returned by ``sync_custom_domains_task``."""

    synced: int
    failed: int


@shared_task(name="events.sync_custom_domains")
def sync_custom_domains_task() -> SyncCustomDomainsResult:
    """Poll Cloudflare for every pending, provisioned custom domain and apply the resulting transition.

    Runs every 15 minutes via Celery Beat. A no-op when Cloudflare is not configured. One domain's
    Cloudflare error is logged and does not stop the others.

    Returns:
        How many domains were synced and how many hit a Cloudflare error.
    """
    result = SyncCustomDomainsResult(synced=0, failed=0)
    if not cloudflare_custom_hostnames.is_configured():
        return result
    # list(): the set is tiny and each sync commits, which must not interleave with a server-side cursor.
    domains = list(
        OrganizationDomain.objects.filter(
            status=OrganizationDomain.Status.PENDING,
            cloudflare_hostname_id__isnull=False,
            organization__custom_domain_enabled=True,
        ).select_related("organization")
    )
    for domain in domains:
        try:
            custom_domain_service.sync_domain_status(domain)
        except CloudflareAPIError:
            logger.exception("custom_domain_sync_failed", domain_id=str(domain.pk))
            result["failed"] += 1
        else:
            result["synced"] += 1
    return result
