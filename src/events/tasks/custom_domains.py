"""Celery tasks for custom-domain provisioning (white-label, #1151)."""

import typing as t

import structlog
from celery import shared_task
from django.db.models import Q

from accounts.models import RevelUser
from common.tasks import send_email
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


@shared_task(name="events.notify_custom_domain_problem")
def notify_custom_domain_problem(domain_id: str, event: str) -> None:
    """Email platform staff that a live custom domain developed a problem or failed.

    Dispatched by ``sync_domain_status`` on the clean -> problem transition of an ACTIVE domain, or when
    it fails. Staff are every active superuser or staff user; the mail goes out as one BCC'd message.

    Args:
        domain_id: The OrganizationDomain id.
        event: ``custom_domain_active_problem`` or ``custom_domain_active_failed``.
    """
    domain = OrganizationDomain.objects.select_related("organization").filter(pk=domain_id).first()
    if domain is None:
        return
    recipients = list(
        RevelUser.objects.filter(Q(is_superuser=True) | Q(is_staff=True), is_active=True)
        .values_list("email", flat=True)
        .distinct()
    )
    if not recipients:
        logger.warning("custom_domain_alert_no_recipients", domain_id=domain_id)
        return
    failed = event == "custom_domain_active_failed"
    headline = "Custom domain failed" if failed else "Custom domain has a problem"
    problems = "\n".join(f"- {message}" for message in domain.last_errors) or "- (none reported)"
    body = (
        f"{headline}: {domain.hostname}\n"
        f"Organization: {domain.organization.name} ({domain.organization_id})\n"
        f"Status: {domain.status}\n\n"
        f"Reported by Cloudflare:\n{problems}\n\n"
        "Review it in the Django admin (Organization domains)."
    )
    send_email(to=recipients, subject=f"{headline}: {domain.hostname}", body=body)
