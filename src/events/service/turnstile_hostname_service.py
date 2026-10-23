"""Keep the Turnstile registration widget's hostname allowlist in step with the ACTIVE custom domains (#1148).

Signup on a white-label domain needs that hostname on the widget; otherwise the captcha fails. This is a
*reconciliation*, not a stream of adds and removes: :func:`reconcile_widget_hostnames` computes the list the
widget should have and writes it only when it differs from what Cloudflare has, so it is idempotent and heals
drift (a hand-edit, a missed event).

Desired widget hostnames = every current entry that Revel does NOT manage (this keeps platform entries such as
``letsrevel.io``) plus the hostnames of ACTIVE domains of organizations with the kill switch on. A hostname is
"Revel-managed" when it is the hostname of any ``OrganizationDomain`` row, in any status, so a domain that is
failed, removed or switched off drops out of the widget while a foreign entry is never touched.

Concurrency: two reconciles can race (read, then write the stale list). That is tolerated: the next one, at
worst the 15-minute sweep, converges, because every run recomputes the full desired list.
"""

import typing as t

import structlog
from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from events.exceptions import CloudflareAPIError
from events.models import OrganizationDomain
from events.service import cloudflare_turnstile

logger = structlog.get_logger(__name__)

API_ERROR_ALERT = "api_error"
OVER_CAPACITY_ALERT = "over_capacity"
# A persisting problem re-alerts at most this often; a successful run resets it, so a later failure alerts again.
ALERT_COOLDOWN_SECONDS = 6 * 60 * 60


class ReconcileResult(t.TypedDict):
    """Outcome of :func:`reconcile_widget_hostnames`."""

    status: t.Literal["disabled", "unchanged", "updated", "error"]
    overflow: int  # active hostnames that did not fit on the widget


def schedule_reconcile() -> None:
    """Reconcile the widget once the current transaction commits. A no-op when Turnstile automation is off.

    Call it wherever a domain becomes ACTIVE or stops being usable (left ACTIVE, kill switch flipped,
    organization deleted). The task runs in Celery, so the caller never waits on or fails because of Cloudflare.
    """
    if not cloudflare_turnstile.is_configured():
        return
    from events.tasks.custom_domains import reconcile_turnstile_hostnames

    transaction.on_commit(lambda: reconcile_turnstile_hostnames.delay())


def _alert_once(kind: str, detail: str) -> None:
    """Email platform staff about a widget problem, at most once per cooldown (see ``ALERT_COOLDOWN_SECONDS``)."""
    if cache.add(f"turnstile_hostnames_alert:{kind}", 1, ALERT_COOLDOWN_SECONDS):
        from events.tasks.custom_domains import notify_turnstile_sync_problem

        notify_turnstile_sync_problem.delay(kind, detail)


def _desired_domains(current: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(desired, overflow)``: the widget list to write, and ACTIVE hostnames that do not fit on it."""
    managed = {h.lower() for h in OrganizationDomain.objects.values_list("hostname", flat=True)}
    wanted = list(
        OrganizationDomain.objects.filter(
            status=OrganizationDomain.Status.ACTIVE, organization__custom_domain_enabled=True
        )
        .order_by("verified_at", "hostname")  # the longest-serving domains win a scarce slot
        .values_list("hostname", flat=True)
    )
    wanted_set = set(wanted)
    kept = [d for d in current if d.lower() not in managed or d.lower() in wanted_set]
    present = {d.lower() for d in kept}
    missing = [h for h in dict.fromkeys(wanted) if h not in present]
    limit = settings.TURNSTILE_WIDGET_HOSTNAME_LIMIT
    slots = max(limit - len(kept), 0) if limit else len(missing)
    return [*kept, *missing[:slots]], missing[slots:]


def reconcile_widget_hostnames() -> ReconcileResult:
    """Make the Turnstile widget's hostname list match the ACTIVE custom domains. Never raises on Cloudflare errors.

    A Cloudflare failure is logged, staff are alerted (once per cooldown) and the result says ``error``;
    nothing is raised, so neither activating a domain nor the sync sweep is ever blocked by it. When more
    ACTIVE hostnames exist than the widget can hold, the ones that fit are written and staff are alerted.
    """
    if not cloudflare_turnstile.is_configured():
        return ReconcileResult(status="disabled", overflow=0)
    try:
        widget = cloudflare_turnstile.get_widget()
        desired, overflow = _desired_domains(widget.domains)  # read the DB after the fetch: smallest stale window
        changed = {d.lower() for d in desired} != {d.lower() for d in widget.domains}
        if changed:
            cloudflare_turnstile.set_widget_domains(widget, desired)
    except CloudflareAPIError as exc:
        logger.error("turnstile_hostnames_sync_failed", detail=str(exc), status_code=exc.status_code)
        _alert_once(API_ERROR_ALERT, str(exc))
        return ReconcileResult(status="error", overflow=0)
    cache.delete(f"turnstile_hostnames_alert:{API_ERROR_ALERT}")
    if overflow:
        logger.error("turnstile_hostnames_over_capacity", overflow=len(overflow), hostnames=overflow)
        _alert_once(OVER_CAPACITY_ALERT, ", ".join(overflow))
    else:
        cache.delete(f"turnstile_hostnames_alert:{OVER_CAPACITY_ALERT}")
    if changed:
        logger.info(
            "turnstile_hostnames_reconciled",
            added=sorted(set(desired) - set(widget.domains)),
            removed=sorted(set(widget.domains) - set(desired)),
        )
    return ReconcileResult(status="updated" if changed else "unchanged", overflow=len(overflow))
