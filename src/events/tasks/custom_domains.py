"""Celery tasks for custom-domain provisioning (white-label, #1151)."""

import typing as t

import structlog
from celery import shared_task

from events.models import OrganizationDomain
from events.service import cloudflare_custom_hostnames, custom_domain_service

logger = structlog.get_logger(__name__)


class SyncCustomDomainsResult(t.TypedDict):
    """Counters returned by ``sync_custom_domains_task``."""

    synced: int
    failed: int


@shared_task(name="events.sync_custom_domains")
def sync_custom_domains_task() -> SyncCustomDomainsResult:
    """Poll Cloudflare for every pending or active, provisioned custom domain and apply the resulting transition.

    Runs every 15 minutes via Celery Beat. A no-op when Cloudflare is not configured. Any error on
    one domain is logged with the domain id and does not stop the others.

    Returns:
        How many domains were synced and how many raised an error.
    """
    result = SyncCustomDomainsResult(synced=0, failed=0)
    if not cloudflare_custom_hostnames.is_configured():
        return result
    # list(): the set is tiny and each sync commits, which must not interleave with a server-side cursor.
    domains = list(
        OrganizationDomain.objects.filter(
            status__in=[OrganizationDomain.Status.PENDING, OrganizationDomain.Status.ACTIVE],
            cloudflare_hostname_id__isnull=False,
            organization__custom_domain_enabled=True,
        ).select_related("organization")
    )
    for domain in domains:
        try:
            custom_domain_service.sync_domain_status(domain)
        except Exception:
            # Per-item isolation in a sweep (precedent: the org-nudge sweep in ``org_nudge_service``,
            # #1052): whatever one domain raises (a Cloudflare error, a bad result shape, the kill switch
            # flipping mid-sync, a validation error) must not stop the others. Beat re-runs the sweep in
            # 15 minutes.
            logger.exception("custom_domain_sync_failed", domain_id=str(domain.pk))
            result["failed"] += 1
        else:
            result["synced"] += 1
    return result
