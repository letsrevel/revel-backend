"""Celery tasks for custom-domain provisioning (white-label, #1151)."""

import typing as t

import structlog
from celery import shared_task
from django.db.models import Q

from accounts.models import RevelUser
from common.tasks import send_email
from events.models import OrganizationDomain
from events.service import cloudflare_custom_hostnames, custom_domain_service, turnstile_hostname_service

logger = structlog.get_logger(__name__)


class SyncCustomDomainsResult(t.TypedDict):
    """Counters returned by ``sync_custom_domains_task``."""

    synced: int
    failed: int


@shared_task(name="events.sync_custom_domains")
def sync_custom_domains_task() -> SyncCustomDomainsResult:
    """Poll Cloudflare for every pending or active, provisioned custom domain and apply the resulting transition.

    Runs every 15 minutes via Celery Beat. Without Cloudflare for SaaS configured it syncs no domain. Any
    error on one domain is logged with the domain id and does not stop the others. Afterwards, once per
    sweep, stale hostname reservations are released (a PENDING domain that never proved ownership is failed after
    7 days, a FAILED one retired after 7 more; both independent of Cloudflare, see ``custom_domain_service``) and
    the Turnstile widget's hostname list is reconciled (a no-op unless that automation is configured).

    Returns:
        How many domains were synced and how many raised an error.
    """
    result = SyncCustomDomainsResult(synced=0, failed=0)
    if cloudflare_custom_hostnames.is_configured():
        _sync_domains(result)
    custom_domain_service.expire_stale_pending_domains()
    custom_domain_service.release_stale_failed_domains()
    # The reconcile also heals drift, so it runs on every sweep. Cloudflare errors are handled inside it; this
    # guards the rest (DB, cache) so the sweep never fails because of it.
    try:
        turnstile_hostname_service.reconcile_widget_hostnames()
    except Exception:
        logger.exception("turnstile_hostnames_reconcile_crashed")
    return result


def _sync_domains(result: SyncCustomDomainsResult) -> None:
    """Sync every pending or active provisioned domain of an enabled organization, counting into ``result``."""
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


@shared_task(name="events.reconcile_turnstile_hostnames")
def reconcile_turnstile_hostnames() -> turnstile_hostname_service.ReconcileResult:
    """Make the Turnstile widget's hostname list match the ACTIVE custom domains.

    Dispatched on commit when a domain becomes ACTIVE or stops being usable (see
    ``turnstile_hostname_service.schedule_reconcile``); the 15-minute sweep runs it too. Cloudflare errors
    are logged and alerted inside, never raised.
    """
    return turnstile_hostname_service.reconcile_widget_hostnames()


def _staff_emails() -> list[str]:
    """Return the email of every active superuser or staff user: the platform alert audience."""
    return list(
        RevelUser.objects.filter(Q(is_superuser=True) | Q(is_staff=True), is_active=True)
        .values_list("email", flat=True)
        .distinct()
    )


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
    recipients = _staff_emails()
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


@shared_task(name="events.delete_organization_cloudflare_hostnames")
def delete_organization_cloudflare_hostnames(hostname_ids: list[str]) -> None:
    """Delete a deleted organization's custom hostnames at Cloudflare (best effort, never raises on API errors).

    Args:
        hostname_ids: The ``cloudflare_hostname_id`` of each of the organization's domains.
    """
    custom_domain_service.delete_orphaned_cloudflare_hostnames(hostname_ids)


@shared_task(name="events.notify_turnstile_sync_problem")
def notify_turnstile_sync_problem(kind: str, detail: str) -> None:
    """Email platform staff that the Turnstile widget's hostname list could not be kept in step.

    Dispatched (rate-limited) by ``turnstile_hostname_service``. Same audience as the custom-domain alerts.

    Args:
        kind: ``api_error`` (Cloudflare failed) or ``over_capacity`` (more active hostnames than the widget holds).
        detail: Cloudflare's error text, or the hostnames that did not fit.
    """
    recipients = _staff_emails()
    if not recipients:
        logger.warning("turnstile_sync_alert_no_recipients", kind=kind)
        return
    if kind == turnstile_hostname_service.OVER_CAPACITY_ALERT:
        headline = "Turnstile widget is full"
        explanation = (
            "These active custom domains do not fit on the widget's hostname list, so sign-up on them fails: "
            f"{detail}\nRemove a custom domain, lower CUSTOM_DOMAIN_LIMIT, or move to a plan with more hostnames "
            "(then raise TURNSTILE_WIDGET_HOSTNAME_LIMIT)."
        )
    else:
        headline = "Turnstile hostname sync failed"
        explanation = (
            f"Cloudflare reported: {detail}\nCustom domains activated or removed since the last good run are not "
            "yet on the widget. Check CLOUDFLARE_API_TOKEN (account-level Turnstile Sites: Edit), "
            "CLOUDFLARE_ACCOUNT_ID and TURNSTILE_SITE_KEY. The sync retries every 15 minutes."
        )
    send_email(to=recipients, subject=headline, body=f"{headline}\n\n{explanation}")
