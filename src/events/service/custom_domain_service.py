"""Custom-domain lifecycle (white-label, #1152) and Cloudflare provisioning (#1151).

The only writer of ``Organization.custom_domain``, the denormalized hostname of an
organization's ACTIVE domain. Every transition runs in a transaction holding the
organization's row lock, keeps that column in step with the domain status, and drops the
tenant-resolution cache entry once the transaction commits.

Provisioning (``request_domain`` / ``sync_domain_status`` / ``delete_domain``) talks to Cloudflare
only when it is configured (:func:`cloudflare_custom_hostnames.is_configured`); otherwise a domain
is just a row carrying the CNAME instruction and platform staff activate it from the Django admin.
"""

import typing as t

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.exceptions import CloudflareAPIError, CustomDomainConflictError, CustomDomainNotEnabledError
from events.models import Organization, OrganizationDomain, OrganizationDomainStatus
from events.schema.custom_domain import DnsRecordSchema, DnsRecordType
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service.tenant_service import invalidate_tenant_cache
from events.utils.hostname import validate_custom_hostname

logger = structlog.get_logger(__name__)

CUSTOM_DOMAIN_NOT_ENABLED_MESSAGE = _("Custom domains are not enabled for this organization.")

# Cloudflare duplicate-hostname error code (HTTP 409): the hostname already exists in the zone.
CLOUDFLARE_DUPLICATE_HOSTNAME_CODE = 1406
# Cloudflare states, https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/domain-support/hostname-validation/validation-status/
# (``status`` / ``ssl.status`` enums: https://developers.cloudflare.com/api/resources/custom_hostnames/).
# Terminal for a PENDING *or ACTIVE* domain: the hostname was refused (``blocked``) or deleted from the zone
# (``deleted``, which Cloudflare does after ``moved`` lasts 7 days); the certificate was deleted.
FAILED_HOSTNAME_STATUSES = frozenset({"blocked", "deleted"})
FAILED_SSL_STATUSES = frozenset({"deleted"})
# ``moved``: a PENDING hostname exhausted Cloudflare's validation backoff, OR the hostname no longer points to the
# fallback origin. The latter is recoverable (a PATCH restarts validation) until Cloudflare deletes it after 7 days,
# so it fails a PENDING domain but only records a problem on an ACTIVE one (Cloudflare reports ``deleted`` if it
# gives up).
MOVED_HOSTNAME_STATUS = "moved"
# Certificate-pipeline timeouts: they fail a PENDING domain; on an ACTIVE one they are only recorded in ``last_errors``.
TIMED_OUT_SSL_STATUSES = frozenset(
    {"initializing_timed_out", "validation_timed_out", "issuance_timed_out", "deployment_timed_out"}
)


def _lock_organization(domain: OrganizationDomain) -> Organization:
    """Lock the domain's organization row and refresh the domain's status under that lock."""
    organization = Organization.objects.select_for_update().get(pk=domain.organization_id)
    domain.refresh_from_db(fields=["status"])
    domain.organization = organization
    return organization


def _save_status(
    domain: OrganizationDomain,
    organization: Organization,
    status: OrganizationDomainStatus,
    *,
    custom_domain: str | None,
) -> None:
    """Persist the new status and sync the denormalized hostname, then invalidate the cache on commit.

    REMOVED is terminal: it frees the hostname for other organizations, so no transition may leave it.
    The row is written with a queryset update, not ``save()``, so a status change is never held hostage
    to the field validators (a hostname that is no longer valid under current settings must still be
    removable). The conditional unique constraints are the exception: they are checked explicitly, so a
    conflict surfaces as a ``ValidationError`` rather than an ``IntegrityError``.
    """
    if domain.status == OrganizationDomain.Status.REMOVED:
        raise DjangoValidationError(str(_("A removed domain cannot be changed.")))
    previous = (domain.status, domain.verified_at)
    now = timezone.now()
    changes: dict[str, t.Any] = {"status": status, "updated_at": now}
    if status == OrganizationDomain.Status.ACTIVE:
        changes["verified_at"] = now
    for field, value in changes.items():
        setattr(domain, field, value)
    try:
        domain.validate_constraints()
    except DjangoValidationError:
        domain.status, domain.verified_at = previous  # the in-memory row must not claim an unsaved status
        raise
    OrganizationDomain.objects.filter(pk=domain.pk).update(**changes)
    if status == OrganizationDomain.Status.ACTIVE or organization.custom_domain == domain.hostname:
        # Queryset update, for the same reason: unrelated organization fields must not block this.
        Organization.objects.filter(pk=organization.pk).update(custom_domain=custom_domain)
        organization.custom_domain = custom_domain
    hostname = domain.hostname
    transaction.on_commit(lambda: invalidate_tenant_cache(hostname))


def activate_domain(domain: OrganizationDomain) -> OrganizationDomain:
    """Mark a domain ACTIVE and publish its hostname on the organization.

    Args:
        domain: The domain to activate (PENDING, FAILED or already ACTIVE).

    Returns:
        OrganizationDomain: The updated domain.

    Raises:
        CustomDomainNotEnabledError: The platform has not enabled custom domains for the organization.
        DjangoValidationError: The domain was removed (a removed domain cannot come back); the
            organization already has another live (pending or active) domain; or the hostname is
            already in use by another domain.
    """
    with transaction.atomic():
        organization = _lock_organization(domain)
        if not organization.custom_domain_enabled:
            raise CustomDomainNotEnabledError
        _save_status(domain, organization, OrganizationDomain.Status.ACTIVE, custom_domain=domain.hostname)
    return domain


def mark_domain_failed(domain: OrganizationDomain) -> OrganizationDomain:
    """Mark a domain FAILED and withdraw its hostname from the organization if it was published.

    Args:
        domain: The domain that failed verification or provisioning.

    Returns:
        OrganizationDomain: The updated domain.

    Raises:
        DjangoValidationError: The domain was removed; a removed domain cannot change status.
    """
    with transaction.atomic():
        organization = _lock_organization(domain)
        _save_status(domain, organization, OrganizationDomain.Status.FAILED, custom_domain=None)
    return domain


def remove_domain(domain: OrganizationDomain) -> OrganizationDomain:
    """Mark a domain REMOVED (freeing its hostname) and withdraw it from the organization.

    Args:
        domain: The domain to retire.

    Returns:
        OrganizationDomain: The updated domain.

    Raises:
        DjangoValidationError: The domain was already removed.
    """
    with transaction.atomic():
        organization = _lock_organization(domain)
        _save_status(domain, organization, OrganizationDomain.Status.REMOVED, custom_domain=None)
    return domain


# ---- Provisioning (org-admin self-service, #1151) ----


def _live_domain_exists(organization: Organization) -> bool:
    return organization.domains.filter(
        status__in=[OrganizationDomain.Status.PENDING, OrganizationDomain.Status.ACTIVE]
    ).exists()


def _require_enabled(organization: Organization) -> None:
    if not organization.custom_domain_enabled:
        raise CustomDomainNotEnabledError


def _dns_records(hostname: str, hosted: cloudflare.CustomHostname | None) -> list[dict[str, str]]:
    """Build the DNS instructions for an organization: the CNAME plus any TXT records Cloudflare asks for."""
    records: list[tuple[DnsRecordType, str, str]] = []
    if target := settings.CUSTOM_DOMAIN_CNAME_TARGET:
        records.append((DnsRecordType.CNAME, hostname, target))
    if hosted:
        ownership = hosted.ownership_verification
        if ownership and ownership.name and ownership.value:
            records.append((DnsRecordType.TXT, ownership.name, ownership.value))
        for record in hosted.ssl.validation_records if hosted.ssl else []:
            if record.txt_name and record.txt_value:
                records.append((DnsRecordType.TXT, record.txt_name, record.txt_value))
    return [
        DnsRecordSchema(type=type_, name=name, value=value).model_dump(mode="json")
        for type_, name, value in dict.fromkeys(records)  # de-duplicated, order preserved
    ]


def _hosted_errors(hosted: cloudflare.CustomHostname) -> list[str]:
    """Collect the problems Cloudflare reports, plus a note for the states that fail a PENDING domain only.

    The note is what an ACTIVE domain keeps in ``last_errors`` instead of being failed (see
    ``MOVED_HOSTNAME_STATUS`` / ``TIMED_OUT_SSL_STATUSES``).
    """
    messages = (
        [*hosted.verification_errors, *(e.message for e in hosted.ssl.validation_errors if e.message)]
        if hosted.ssl
        else list(hosted.verification_errors)
    )
    ssl_status = hosted.ssl.status if hosted.ssl else None
    if hosted.status == MOVED_HOSTNAME_STATUS:
        messages.append("Cloudflare reports the hostname as 'moved': it no longer points to the fallback origin.")
    if ssl_status in TIMED_OUT_SSL_STATUSES:
        messages.append(f"Cloudflare reports the certificate status '{ssl_status}'.")
    return list(dict.fromkeys(messages))  # de-duplicated, order preserved


def _target_status(current: str, hosted: cloudflare.CustomHostname) -> OrganizationDomainStatus | None:
    """Decide the status a domain in ``current`` (PENDING or ACTIVE) should move to given Cloudflare's state."""
    ssl_status = hosted.ssl.status if hosted.ssl else None
    if hosted.status in FAILED_HOSTNAME_STATUSES or ssl_status in FAILED_SSL_STATUSES:
        return OrganizationDomain.Status.FAILED
    if current == OrganizationDomain.Status.PENDING:
        if hosted.status == MOVED_HOSTNAME_STATUS or ssl_status in TIMED_OUT_SSL_STATUSES:
            return OrganizationDomain.Status.FAILED
        if hosted.status == "active" and ssl_status == "active":
            return OrganizationDomain.Status.ACTIVE
    return None


def get_current_domain(organization: Organization) -> OrganizationDomain | None:
    """Return the organization's latest non-removed domain (pending, active or failed), if any.

    Raises:
        CustomDomainNotEnabledError: The platform has not enabled custom domains for the organization.
    """
    _require_enabled(organization)
    return organization.domains.exclude(status=OrganizationDomain.Status.REMOVED).first()


def get_current_domain_or_404(organization: Organization) -> OrganizationDomain:
    """Like :func:`get_current_domain`, but answer 404 when the organization has no domain."""
    if (domain := get_current_domain(organization)) is None:
        raise HttpError(404, str(_("This organization has no custom domain.")))
    return domain


def _create_cloudflare_hostname(hostname: str) -> cloudflare.CustomHostname:
    """Create the Cloudflare custom hostname, translating a duplicate into a conflict.

    Raises:
        CustomDomainConflictError: Cloudflare already has this hostname.
        CloudflareAPIError: Cloudflare failed. If it created the hostname but its result was unusable,
            the orphan is deleted (best effort) before the error propagates.
    """
    try:
        return cloudflare.create_custom_hostname(hostname)
    except CloudflareAPIError as exc:
        if exc.hostname_id:  # created at Cloudflare, but the result was unusable: do not leave it orphaned
            _delete_cloudflare_hostname_best_effort(exc.hostname_id)
        if exc.status_code == 409 or CLOUDFLARE_DUPLICATE_HOSTNAME_CODE in exc.error_codes:
            raise CustomDomainConflictError(str(_("This hostname is already in use."))) from exc
        raise


def request_domain(organization: Organization, hostname: str) -> OrganizationDomain:
    """Request a custom domain: validate, provision it on Cloudflare (when configured) and store the PENDING row.

    Ordering: the Cloudflare call happens synchronously *before* the row is created, so a Cloudflare
    failure leaves no orphan row (the exception propagates and the request answers 502). If the insert
    then fails (e.g. a concurrent request for the same organization), or the create succeeded but its
    result could not be parsed, the just-created Cloudflare hostname is deleted again. A background task
    would instead leave a PENDING row whose provisioning can silently fail, and ``on_commit`` dispatch
    adds a delay for no benefit: the organizer needs the DNS records in the response anyway.

    Locking: the Cloudflare create runs outside any ``select_for_update`` of its own. But under
    ``ATOMIC_REQUESTS`` the whole request is one transaction, so when a FAILED domain is retired first
    (``delete_domain`` -> ``remove_domain`` takes the organization's row lock) that one organization row
    stays locked until the request commits, i.e. through the Cloudflare create (at most its 10 s timeout).
    With no FAILED domain to retire, no lock is held across the network call.

    A FAILED domain of the organization is retired first, so the organizer can retry without deleting it.

    Args:
        organization: The organization requesting the domain.
        hostname: The hostname as entered.

    Returns:
        OrganizationDomain: The PENDING domain.

    Raises:
        CustomDomainNotEnabledError: The platform has not enabled custom domains for the organization.
        DjangoValidationError: The hostname is not acceptable.
        CustomDomainConflictError: The organization already has a live domain, or the hostname is taken.
        CloudflareAPIError: Cloudflare failed.
    """
    _require_enabled(organization)
    hostname = validate_custom_hostname(hostname)
    if _live_domain_exists(organization):
        raise CustomDomainConflictError(str(_("This organization already has a pending or active domain.")))
    # ponytail: under ATOMIC_REQUESTS this retirement holds the org row lock through the Cloudflare create
    # below (one org row, <= REQUEST_TIMEOUT_SECONDS). The order is deliberate: a same-hostname retry needs
    # the old Cloudflare hostname deleted first. Upgrade path if that ever contends: retire the failed
    # domain in its own committed step before the create.
    for failed in organization.domains.filter(status=OrganizationDomain.Status.FAILED):
        delete_domain(failed)
    if OrganizationDomain.objects.exclude(status=OrganizationDomain.Status.REMOVED).filter(hostname=hostname).exists():
        raise CustomDomainConflictError(str(_("This hostname is already in use.")))

    hosted = _create_cloudflare_hostname(hostname) if cloudflare.is_configured() else None
    try:
        with transaction.atomic():
            # Serialize with other requests of this organization and re-check under the lock.
            locked = Organization.objects.select_for_update().get(pk=organization.pk)
            if _live_domain_exists(locked):
                raise CustomDomainConflictError(str(_("This organization already has a pending or active domain.")))
            return OrganizationDomain.objects.create(
                organization=organization,
                hostname=hostname,
                cloudflare_hostname_id=hosted.id if hosted else None,
                verification_records=_dns_records(hostname, hosted),
            )
    except Exception:
        if hosted:
            _delete_cloudflare_hostname_best_effort(hosted.id)
        raise


def _delete_cloudflare_hostname_best_effort(custom_hostname_id: str) -> None:
    """Undo a Cloudflare creation whose database insert failed; log (not raise) so the original error surfaces."""
    try:
        cloudflare.delete_custom_hostname(custom_hostname_id)
    except CloudflareAPIError:
        logger.exception("custom_domain_orphaned_cloudflare_hostname", cloudflare_hostname_id=custom_hostname_id)


ACTIVE_PROBLEM_EVENT = "custom_domain_active_problem"
ACTIVE_FAILED_EVENT = "custom_domain_active_failed"


def _alert_active_domain_problem(domain: OrganizationDomain, event: str, problems: list[str]) -> None:
    """Log a WARNING and email platform staff: a live (ACTIVE) custom domain developed a problem or failed.

    The caller guards the transition (clean -> problem, or ACTIVE -> FAILED), so a persisting problem
    does not alert again on every 15-minute sweep. The email is dispatched on commit.
    """
    from events.tasks.custom_domains import notify_custom_domain_problem

    logger.warning(
        event,
        domain_id=str(domain.pk),
        hostname=domain.hostname,
        organization_id=str(domain.organization_id),
        problems=problems,
    )
    domain_id = str(domain.pk)
    transaction.on_commit(lambda: notify_custom_domain_problem.delay(domain_id, event))


def sync_domain_status(domain: OrganizationDomain) -> OrganizationDomain:
    """Refresh a PENDING or ACTIVE domain from Cloudflare and apply the resulting transition.

    Hostname and certificate both ``active`` activates a PENDING domain. ``blocked`` / ``deleted`` (hostname)
    and ``deleted`` (certificate) mark a PENDING *or ACTIVE* domain failed, withdrawing it from the
    organization. ``moved`` (hostname) and the certificate ``*_timed_out`` states fail a PENDING domain
    only: an ACTIVE one stays ACTIVE and the problem is recorded in ``last_errors`` (see the constants
    above). Anything else, including transient states such as ``active_redeploying``, leaves the status
    alone. The DNS records and Cloudflare's problem messages (``last_errors``) are refreshed either way.
    When an ACTIVE domain gets a problem it did not have before, or fails, a WARNING is logged and platform
    staff are emailed (``notify_custom_domain_problem``), once per transition.
    A no-op for domains in any other status or never provisioned on Cloudflare.

    The transition is a compare-and-set: the Cloudflare decision was made for the status the domain had
    when it was read, so under the organization lock the status is re-read and, if it changed meanwhile
    (e.g. a concurrent delete REMOVED it, or a concurrent sync already moved it), nothing is applied and
    the current state is returned.

    Args:
        domain: The domain to refresh.

    Returns:
        OrganizationDomain: The domain, updated in place.

    Raises:
        CloudflareAPIError: Cloudflare failed.
        CustomDomainNotEnabledError: The domain became ready but the platform disabled custom domains.
    """
    expected = domain.status
    if expected not in (OrganizationDomain.Status.PENDING, OrganizationDomain.Status.ACTIVE):
        return domain
    if not domain.cloudflare_hostname_id:
        return domain
    hosted = cloudflare.get_custom_hostname(domain.cloudflare_hostname_id)
    records = _dns_records(domain.hostname, hosted)
    errors = _hosted_errors(hosted)
    # A live domain developing a problem is only visible here: alert once, on the clean -> problem edge.
    new_active_problem = expected == OrganizationDomain.Status.ACTIVE and bool(errors) and not domain.last_errors
    if (records, errors) != (domain.verification_records, domain.last_errors):
        # Queryset update, never ``save()``: that would run full_clean and re-run the hostname validator
        # (mirrors ``_save_status``). Filtered on the status the decision is based on, so a row that was
        # removed meanwhile is left untouched.
        OrganizationDomain.objects.filter(pk=domain.pk, status=expected).update(
            verification_records=records, last_errors=errors, updated_at=timezone.now()
        )
        domain.verification_records, domain.last_errors = records, errors
    target = _target_status(expected, hosted)
    if target is None:
        if new_active_problem:
            _alert_active_domain_problem(domain, ACTIVE_PROBLEM_EVENT, errors)
        return domain
    with transaction.atomic():
        _lock_organization(domain)  # also re-reads domain.status under the lock
        if domain.status != expected:
            logger.info(
                "custom_domain_sync_transition_skipped",
                domain_id=str(domain.pk),
                expected_status=expected,
                current_status=domain.status,
                target_status=target,
            )
            return domain
        if target == OrganizationDomain.Status.ACTIVE:
            return activate_domain(domain)
        mark_domain_failed(domain)
    if expected == OrganizationDomain.Status.ACTIVE:
        _alert_active_domain_problem(domain, ACTIVE_FAILED_EVENT, errors)
    return domain


def delete_domain(domain: OrganizationDomain) -> OrganizationDomain:
    """Delete the domain's Cloudflare custom hostname (a 404 counts as already gone) and mark it REMOVED.

    When the domain has a Cloudflare hostname but Cloudflare is no longer configured, it is removed
    locally anyway and a warning is logged, since the hostname may be orphaned at Cloudflare.

    Raises:
        CloudflareAPIError: Cloudflare failed with anything other than "not found".
    """
    if domain.cloudflare_hostname_id and cloudflare.is_configured():
        try:
            cloudflare.delete_custom_hostname(domain.cloudflare_hostname_id)
        except CloudflareAPIError as exc:
            if exc.status_code != 404:
                raise
    elif domain.cloudflare_hostname_id:
        logger.warning(
            "custom_domain_cloudflare_not_configured_on_delete",
            domain_id=str(domain.pk),
            cloudflare_hostname_id=domain.cloudflare_hostname_id,
            detail="Cloudflare is not configured; the custom hostname may be orphaned there.",
        )
    return remove_domain(domain)
