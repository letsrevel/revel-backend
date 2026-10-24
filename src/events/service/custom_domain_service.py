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
from datetime import timedelta

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.exceptions import CloudflareAPIError, CustomDomainConflictError, CustomDomainNotEnabledError
from events.models import Organization, OrganizationDomain, OrganizationDomainStatus
from events.schema.custom_domain import DnsRecordSchema, DnsRecordType
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import custom_domain_notifications, dns_diagnostics, turnstile_hostname_service
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
# fallback origin. Either way the domain fails, ACTIVE ones included: DNS no longer reaches us, so the domain is not
# serving our site anyway (the organization owner may have repointed it at a server of their own), and failing it
# withdraws the hostname, which signs out every session issued on it (``accounts.service.tenant_session``). It is
# recoverable: re-point the DNS and request the domain again.
MOVED_HOSTNAME_STATUS = "moved"
# Certificate-pipeline timeouts: they fail a PENDING domain; on an ACTIVE one (DNS still ours) they are only
# recorded in ``last_errors``.
TIMED_OUT_SSL_STATUSES = frozenset(
    {"initializing_timed_out", "validation_timed_out", "issuance_timed_out", "deployment_timed_out"}
)

# How long a hostname may be held without being proven or fixed. A PENDING domain that has not become ACTIVE
# (its ownership was never proven) is failed after PENDING_EXPIRY, and a FAILED one is retired (REMOVED, freeing
# the hostname for anyone else) after FAILED_RETENTION. 7 days mirrors Cloudflare, which deletes a ``moved``
# hostname after the same window, and is generous for DNS propagation and a customer's change process.
PENDING_EXPIRY = timedelta(days=7)
FAILED_RETENTION = timedelta(days=7)
PENDING_EXPIRED_MESSAGE = "Ownership of the hostname was not proven within 7 days."


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
    if OrganizationDomain.Status.ACTIVE in (previous[0], status):
        turnstile_hostname_service.schedule_reconcile()  # the domain entered or left the Turnstile hostname list
    custom_domain_notifications.notify_owner_of_transition(domain, organization, previous[0], status)


def restart_domain_sessions(organization: Organization) -> None:
    """Sign out every session issued on the organization's active domain before now.

    Re-enabling the ``custom_domain_enabled`` kill switch must not revive sessions issued before it was
    switched off: the host resolves again, and a refresh token that was not presented while it was off would
    otherwise be accepted. ``verified_at`` is the "sessions must postdate this" mark the refresh check uses
    (``accounts.service.tenant_session``), so it is moved to now. Queryset update, never ``save()``, for the
    same reason as in ``_save_status``. A no-op when the organization has no ACTIVE domain.

    Side effects: the owner's page shows the re-enable time as ``verified_at``, and the Turnstile slot
    priority (``turnstile_hostname_service`` orders by ``verified_at``) treats the domain as the newest.

    Args:
        organization: The organization whose kill switch was just switched on.
    """
    # ponytail: reuses ``verified_at`` as the sessions-valid-after mark. The Turnstile side effect is harmless
    # while CUSTOM_DOMAIN_LIMIT < TURNSTILE_WIDGET_HOSTNAME_LIMIT; upgrade path: a separate field.
    OrganizationDomain.objects.filter(organization=organization, status=OrganizationDomain.Status.ACTIVE).update(
        verified_at=timezone.now()
    )


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


# Arbitrary constant: the key of the Postgres advisory lock that serializes the platform-wide domain limit check.
_DOMAIN_LIMIT_LOCK_KEY = 1148_01


def _domain_limit_reached() -> bool:
    """Return whether the platform already has ``CUSTOM_DOMAIN_LIMIT`` live (pending or active) custom domains.

    The limit exists because every ACTIVE domain takes a slot on the Turnstile widget (Free plan: 10 per widget,
    one taken by the platform host), so it counts live domains platform-wide, whatever the organization's kill
    switch says (a switched-off domain returns to the widget when the switch is turned back on). FAILED and
    REMOVED domains do not count. ``0`` means unlimited.
    """
    limit = settings.CUSTOM_DOMAIN_LIMIT
    if not limit:
        return False
    live = OrganizationDomain.objects.filter(
        status__in=[OrganizationDomain.Status.PENDING, OrganizationDomain.Status.ACTIVE]
    )
    return live.count() >= limit


def _raise_if_domain_limit_reached() -> None:
    if _domain_limit_reached():
        raise CustomDomainConflictError(
            str(_("The platform has reached its limit of custom domains. Please contact support."))
        )


def _require_enabled(organization: Organization) -> None:
    if not organization.custom_domain_enabled:
        raise CustomDomainNotEnabledError


def _ownership_record(
    hostname: str, hosted: cloudflare.CustomHostname | None, stored: list[dict[str, str]]
) -> tuple[str, str] | None:
    """Return the ``(name, value)`` of the ownership TXT record Cloudflare issued for this hostname, if any.

    Cloudflare returns ``ownership_verification`` only until it has verified the hostname, then drops it from
    the API response. With HTTP validation that takes minutes, so a sync usually first sees the hostname ready
    *without* the record. The copy stored in ``verification_records`` stands in: a row's Cloudflare hostname is
    created once, with the row, so the stored record was issued for that same hostname and proves the same thing.
    """
    ownership = hosted.ownership_verification if hosted else None
    if ownership and ownership.name and ownership.value:
        return ownership.name, ownership.value
    name = f"{dns_diagnostics.OWNERSHIP_TXT_LABEL}.{hostname}"
    return next(
        (
            (r["name"], r["value"])
            for r in stored
            if r["type"] == DnsRecordType.TXT and r["name"] == name and r["value"]
        ),
        None,
    )


def _dns_records(
    hostname: str, hosted: cloudflare.CustomHostname | None, stored: list[dict[str, str]] | None = None
) -> list[dict[str, str]]:
    """Build the DNS instructions for an organization: the CNAME plus any TXT records Cloudflare asks for.

    ``stored`` are the instructions saved so far; they keep the ownership TXT record listed once Cloudflare
    stops returning it (see :func:`_ownership_record`).
    """
    records: list[tuple[DnsRecordType, str, str]] = []
    if target := settings.CUSTOM_DOMAIN_CNAME_TARGET:
        records.append((DnsRecordType.CNAME, hostname, target))
    if ownership := _ownership_record(hostname, hosted, stored or []):
        records.append((DnsRecordType.TXT, *ownership))
    if hosted:
        for record in hosted.ssl.validation_records if hosted.ssl else []:
            if record.txt_name and record.txt_value:
                records.append((DnsRecordType.TXT, record.txt_name, record.txt_value))
    return [
        DnsRecordSchema(type=type_, name=name, value=value).model_dump(mode="json")
        for type_, name, value in dict.fromkeys(records)  # de-duplicated, order preserved
    ]


def _hosted_errors(hosted: cloudflare.CustomHostname) -> list[str]:
    """Collect the problems Cloudflare reports, plus a note for ``moved`` and the certificate timeouts.

    The timeout note is what an ACTIVE domain keeps in ``last_errors`` instead of being failed (see
    ``TIMED_OUT_SSL_STATUSES``).
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


def _ownership_error(domain: OrganizationDomain, hosted: cloudflare.CustomHostname) -> str | None:
    """Return why a domain Cloudflare reports ready may not be activated yet, or ``None`` once ownership is proven.

    Cloudflare activates a hostname on its CNAME alone (HTTP validation), without the ownership TXT record, so
    a dangling CNAME of a removed domain would let another organization claim the hostname. Activation
    therefore also requires that TXT record to be published in public DNS. The record is Cloudflare's current
    ``ownership_verification`` or, once Cloudflare has dropped it, the stored copy (:func:`_ownership_record`).
    Fails closed: no record either way, or a failed lookup, keeps the domain PENDING.
    """
    ownership = _ownership_record(domain.hostname, hosted, domain.verification_records)
    if ownership is None:
        logger.warning(
            "custom_domain_ownership_record_missing", hostname=domain.hostname, cloudflare_hostname_id=hosted.id
        )
        return "Cloudflare has not issued the ownership TXT record for this hostname yet."
    name, value = ownership
    if cloudflare.txt_record_published(name, value):
        return None
    return f"Add the TXT record {name} with value {value} to prove you own this hostname."


def _target_status(current: str, hosted: cloudflare.CustomHostname) -> OrganizationDomainStatus | None:
    """Decide the status a domain in ``current`` (PENDING or ACTIVE) should move to given Cloudflare's state.

    ACTIVE here only means Cloudflare is ready; ``sync_domain_status`` additionally requires the ownership
    TXT record (see :func:`_ownership_error`) before it activates a PENDING domain.
    """
    ssl_status = hosted.ssl.status if hosted.ssl else None
    if hosted.status in FAILED_HOSTNAME_STATUSES or ssl_status in FAILED_SSL_STATUSES:
        return OrganizationDomain.Status.FAILED
    if hosted.status == MOVED_HOSTNAME_STATUS:
        return OrganizationDomain.Status.FAILED
    if current == OrganizationDomain.Status.PENDING:
        if ssl_status in TIMED_OUT_SSL_STATUSES:
            return OrganizationDomain.Status.FAILED
        if hosted.status == "active" and ssl_status == "active":
            return OrganizationDomain.Status.ACTIVE
    return None


def _gate_and_diagnose(
    domain: OrganizationDomain, expected: str, hosted: cloudflare.CustomHostname
) -> tuple[list[str], OrganizationDomainStatus | None]:
    """Return ``(errors, target)`` for a sync: Cloudflare's problems, the ownership gate and the DNS pre-flight hints.

    The target is Cloudflare's decision, held back at PENDING (``None``) while the ownership TXT is not published.
    A PENDING domain that is not becoming ACTIVE also gets :func:`dns_diagnostics.diagnose_dns`; when Cloudflare
    is ready and only the TXT is missing, only the mistake that explains it is checked (``ownership_only``).
    """
    errors = _hosted_errors(hosted)
    target = _target_status(expected, hosted)
    ownership_only = False
    if target == OrganizationDomain.Status.ACTIVE and (proof_error := _ownership_error(domain, hosted)):
        errors, target = [*errors, proof_error], None  # ready at Cloudflare, but ownership is not proven: stay PENDING
        ownership_only = True
    if expected == OrganizationDomain.Status.PENDING and target != OrganizationDomain.Status.ACTIVE:
        hints = dns_diagnostics.diagnose_dns(domain.hostname, ownership_only=ownership_only)
        errors = list(dict.fromkeys([*errors, *hints]))  # de-duplicated, order preserved
    return errors, target


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
    With no FAILED domain to retire, no lock is held across the Cloudflare call. The platform-wide advisory lock
    is taken last, and the DNS provider lookup that the response schema needs is made (and cached) before any
    lock, so the lock is never held across a DNS-over-HTTPS lookup either.

    A FAILED domain of the organization is retired first, so the organizer can retry without deleting it.

    Args:
        organization: The organization requesting the domain.
        hostname: The hostname as entered.

    Returns:
        OrganizationDomain: The PENDING domain.

    Raises:
        CustomDomainNotEnabledError: The platform has not enabled custom domains for the organization.
        DjangoValidationError: The hostname is not acceptable.
        CustomDomainConflictError: The organization already has a live domain, the hostname is taken, or the
            platform is at ``CUSTOM_DOMAIN_LIMIT`` live domains.
        CloudflareAPIError: Cloudflare failed.
    """
    _require_enabled(organization)
    try:
        hostname = validate_custom_hostname(hostname)
    except DjangoValidationError as exc:
        # Keyed by the request field, so the API answers {"errors": {"hostname": [...]}}.
        raise DjangoValidationError({"hostname": exc.messages}) from exc
    # Warm the provider cache the 201 response reads (``CustomDomainSchema.resolve_dns_provider``), so the DoH
    # lookup never runs later while the advisory lock below is held for the rest of the request.
    dns_diagnostics.detect_dns_provider(hostname)
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
    _raise_if_domain_limit_reached()  # fail fast, before creating anything at Cloudflare; re-checked under lock below

    hosted = _create_cloudflare_hostname(hostname) if cloudflare.is_configured() else None
    try:
        with transaction.atomic():
            # Serialize with other requests of this organization and re-check under the lock.
            locked = Organization.objects.select_for_update().get(pk=organization.pk)
            if _live_domain_exists(locked):
                raise CustomDomainConflictError(str(_("This organization already has a pending or active domain.")))
            # Platform-wide limit: an advisory lock (held to the end of the transaction) makes check + insert
            # exact across organizations, which the per-organization row lock above cannot.
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_DOMAIN_LIMIT_LOCK_KEY])
            _raise_if_domain_limit_reached()
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


def delete_orphaned_cloudflare_hostnames(hostname_ids: list[str]) -> None:
    """Delete the Cloudflare hostnames of a deleted organization, best effort.

    Deleting an organization cascades its domain rows without going through :func:`delete_domain`, so
    their Cloudflare hostnames would be orphaned (and keep the certificates and the hostname claimed).
    A 404 counts as already gone; any other failure is logged, never raised, and Cloudflare not being
    configured is a logged skip, so nothing here can block or fail the organization's deletion.
    """
    if not hostname_ids:
        return
    if not cloudflare.is_configured():
        logger.warning(
            "custom_domain_cloudflare_not_configured_on_organization_delete",
            cloudflare_hostname_ids=hostname_ids,
            detail="Cloudflare is not configured; the custom hostnames may be orphaned there.",
        )
        return
    for hostname_id in hostname_ids:
        try:
            cloudflare.delete_custom_hostname(hostname_id)
        except CloudflareAPIError as exc:
            if exc.status_code != 404:
                logger.exception("custom_domain_orphaned_cloudflare_hostname", cloudflare_hostname_id=hostname_id)


def sync_domain_status(domain: OrganizationDomain) -> OrganizationDomain:
    """Refresh a PENDING or ACTIVE domain from Cloudflare and apply the resulting transition.

    Hostname and certificate both ``active`` activates a PENDING domain, but only once the ownership TXT
    record Cloudflare issued (``ownership_verification``) is published in public DNS; until then the domain
    stays PENDING and ``last_errors`` says which record to add (ACTIVE domains are never re-checked).
    A domain that was PENDING and is not becoming ACTIVE on this run also gets the DNS pre-flight hints of
    :func:`dns_diagnostics.diagnose_dns` appended to ``last_errors`` (never for ACTIVE ones: a new ``last_errors``
    entry there alerts platform staff). When Cloudflare reports the hostname ready and only the ownership TXT is
    missing, the message above already says what to add, so only the one mistake that explains it (records at
    the doubled name) is checked. The sweep and **Check now** both come through here.
    ``blocked`` / ``deleted`` (hostname) and ``deleted`` (certificate) mark a PENDING *or ACTIVE* domain
    failed, withdrawing it from the organization. So does ``moved`` (hostname): the DNS no longer points at us,
    and failing the domain signs out every session issued on it. The certificate ``*_timed_out`` states fail a
    PENDING domain only: an ACTIVE one stays ACTIVE and the problem is recorded in ``last_errors`` (see the
    constants above). Anything else, including transient states such as ``active_redeploying``, leaves the
    status alone.
    The DNS records and Cloudflare's problem messages (``last_errors``) are refreshed either way.
    When an ACTIVE domain gets a problem it did not have before, or fails, a WARNING is logged and platform
    staff are emailed (``notify_custom_domain_problem``), once per transition.
    A no-op for domains in any other status, never provisioned on Cloudflare, or when Cloudflare is not configured.

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
    if not domain.cloudflare_hostname_id or not cloudflare.is_configured():
        return domain
    hosted = cloudflare.get_custom_hostname(domain.cloudflare_hostname_id)
    records = _dns_records(domain.hostname, hosted, domain.verification_records)
    errors, target = _gate_and_diagnose(domain, expected, hosted)
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


# ---- Lifecycle sweeps: release hostnames that are never proven or fixed (#1168) ----


def _expire_pending_domain(domain: OrganizationDomain) -> bool:
    """Fail a PENDING domain with the expiry message; ``False`` when it was no longer PENDING under the lock."""
    with transaction.atomic():
        _lock_organization(domain)  # also re-reads domain.status under the lock
        if domain.status != OrganizationDomain.Status.PENDING:
            return False
        # Queryset update, never ``save()`` (see ``_save_status``). ``last_errors`` is set before the transition
        # so the owner's notification carries the reason.
        errors = list(dict.fromkeys([*domain.last_errors, PENDING_EXPIRED_MESSAGE]))
        OrganizationDomain.objects.filter(pk=domain.pk).update(last_errors=errors, updated_at=timezone.now())
        domain.last_errors = errors
        mark_domain_failed(domain)
    return True


def expire_stale_pending_domains() -> int:
    """Fail every PENDING domain requested more than ``PENDING_EXPIRY`` ago, so it stops holding its hostname.

    Nothing else ends a PENDING row whose owner never publishes the DNS records, and a PENDING row reserves the
    hostname against every other organization. Goes through :func:`mark_domain_failed`, so the owner is told and
    the tenant cache and Turnstile list are kept in step. Like the Cloudflare sync it skips organizations whose
    ``custom_domain_enabled`` kill switch is off. An error on one domain is logged and does not stop the others.

    Returns:
        How many domains were failed.
    """
    stale = list(
        OrganizationDomain.objects.filter(
            status=OrganizationDomain.Status.PENDING,
            created_at__lt=timezone.now() - PENDING_EXPIRY,
            organization__custom_domain_enabled=True,
        ).select_related("organization")
    )
    expired = 0
    for domain in stale:
        try:
            expired += _expire_pending_domain(domain)
        except Exception:
            # Per-item isolation in a sweep, as in ``sync_custom_domains_task``: Beat retries in 15 minutes.
            logger.exception("custom_domain_expiry_failed", domain_id=str(domain.pk))
    return expired


def release_stale_failed_domains() -> int:
    """Retire (REMOVED) every FAILED domain unchanged for ``FAILED_RETENTION``, freeing its hostname.

    FAILED rows keep holding the hostname (``unique_hostname_among_non_removed``) and their Cloudflare custom
    hostname, which would answer a duplicate with a 409 to the hostname's real owner. This reuses
    :func:`delete_domain`: the Cloudflare hostname is deleted when Cloudflare is configured (a 404 counts as
    gone), then the row is removed. When Cloudflare refuses the deletion the row stays FAILED, so the hostname
    is not freed while it is still claimed there, and the next sweep retries. ``updated_at`` is when the domain
    last changed status, which is when it failed. Skips organizations whose kill switch is off, like the
    Cloudflare sync, and isolates errors per domain.

    Returns:
        How many domains were retired.
    """
    stale = list(
        OrganizationDomain.objects.filter(
            status=OrganizationDomain.Status.FAILED,
            updated_at__lt=timezone.now() - FAILED_RETENTION,
            organization__custom_domain_enabled=True,
        ).select_related("organization")
    )
    released = 0
    for domain in stale:
        try:
            delete_domain(domain)
        except Exception:
            logger.exception("custom_domain_release_failed", domain_id=str(domain.pk))
        else:
            released += 1
    return released
