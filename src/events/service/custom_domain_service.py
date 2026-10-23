"""Custom-domain lifecycle (white-label, #1152) and Cloudflare provisioning (#1151).

The only writer of ``Organization.custom_domain``, the denormalized hostname of an
organization's ACTIVE domain. Every transition runs in a transaction holding the
organization's row lock, keeps that column in step with the domain status, and drops the
tenant-resolution cache entry once the transaction commits.

Provisioning (``request_domain`` / ``sync_domain_status`` / ``delete_domain``) talks to Cloudflare
only when it is configured (:func:`cloudflare_custom_hostnames.is_configured`); otherwise a domain
is just a row carrying the CNAME instruction and platform staff activate it from the Django admin.
"""

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.exceptions import CloudflareAPIError, CustomDomainConflictError, CustomDomainNotEnabledError
from events.models import Organization, OrganizationDomain
from events.schema.custom_domain import DnsRecordSchema, DnsRecordType
from events.service import cloudflare_custom_hostnames as cloudflare
from events.service.tenant_service import invalidate_tenant_cache
from events.utils.hostname import validate_custom_hostname

logger = structlog.get_logger(__name__)

CUSTOM_DOMAIN_NOT_ENABLED_MESSAGE = _("Custom domains are not enabled for this organization.")

# Cloudflare duplicate-hostname error code (HTTP 409): the hostname already exists in the zone.
CLOUDFLARE_DUPLICATE_HOSTNAME_CODE = 1406
# Terminal Cloudflare states: the hostname/certificate will not become active without a fresh request.
# Docs: https://developers.cloudflare.com/api/resources/custom_hostnames/ (``status`` / ``ssl.status`` enums).
FAILED_HOSTNAME_STATUSES = frozenset({"blocked", "moved", "deleted"})
FAILED_SSL_STATUSES = frozenset(
    {
        "deleted",
        "initializing_timed_out",
        "validation_timed_out",
        "issuance_timed_out",
        "deployment_timed_out",
    }
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
    status: OrganizationDomain.Status,
    *,
    custom_domain: str | None,
) -> None:
    """Persist the new status and sync the denormalized hostname, then invalidate the cache on commit."""
    domain.status = status
    update_fields = ["status", "updated_at"]
    if status == OrganizationDomain.Status.ACTIVE:
        domain.verified_at = timezone.now()
        update_fields.append("verified_at")
    domain.save(update_fields=update_fields)
    if status == OrganizationDomain.Status.ACTIVE or organization.custom_domain == domain.hostname:
        # Queryset update: it bypasses ``save()``'s full_clean, which must not hold a
        # status change hostage to unrelated organization fields.
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
        DjangoValidationError: The domain was removed; a removed domain cannot come back.
    """
    with transaction.atomic():
        organization = _lock_organization(domain)
        if not organization.custom_domain_enabled:
            raise CustomDomainNotEnabledError
        if domain.status == OrganizationDomain.Status.REMOVED:
            raise DjangoValidationError(str(_("A removed domain cannot be activated.")))
        _save_status(domain, organization, OrganizationDomain.Status.ACTIVE, custom_domain=domain.hostname)
    return domain


def mark_domain_failed(domain: OrganizationDomain) -> OrganizationDomain:
    """Mark a domain FAILED and withdraw its hostname from the organization if it was published.

    Args:
        domain: The domain that failed verification or provisioning.

    Returns:
        OrganizationDomain: The updated domain.
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


def request_domain(organization: Organization, hostname: str) -> OrganizationDomain:
    """Request a custom domain: validate, provision it on Cloudflare (when configured) and store the PENDING row.

    Ordering: the Cloudflare call happens synchronously *before* the row is created, so a Cloudflare
    failure leaves no orphan row (the exception propagates and the request answers 502). It holds no
    database lock while waiting on the network. If the insert then fails (e.g. a concurrent request for
    the same organization), the just-created Cloudflare hostname is deleted again. A background task
    would instead leave a PENDING row whose provisioning can silently fail, and ``on_commit`` dispatch
    adds a delay for no benefit: the organizer needs the DNS records in the response anyway.

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
    for failed in organization.domains.filter(status=OrganizationDomain.Status.FAILED):
        delete_domain(failed)
    if OrganizationDomain.objects.exclude(status=OrganizationDomain.Status.REMOVED).filter(hostname=hostname).exists():
        raise CustomDomainConflictError(str(_("This hostname is already in use.")))

    hosted: cloudflare.CustomHostname | None = None
    if cloudflare.is_configured():
        try:
            hosted = cloudflare.create_custom_hostname(hostname)
        except CloudflareAPIError as exc:
            if exc.status_code == 409 or CLOUDFLARE_DUPLICATE_HOSTNAME_CODE in exc.error_codes:
                raise CustomDomainConflictError(str(_("This hostname is already in use."))) from exc
            raise
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


def sync_domain_status(domain: OrganizationDomain) -> OrganizationDomain:
    """Refresh a PENDING domain from Cloudflare and apply the resulting transition.

    Hostname and certificate both ``active`` activates the domain; a terminal Cloudflare state
    (see ``FAILED_HOSTNAME_STATUSES`` / ``FAILED_SSL_STATUSES``) marks it failed; anything else keeps it
    pending. A no-op for domains that are not PENDING or were never provisioned on Cloudflare.

    Args:
        domain: The domain to refresh.

    Returns:
        OrganizationDomain: The domain, updated in place.

    Raises:
        CloudflareAPIError: Cloudflare failed.
        CustomDomainNotEnabledError: The domain became ready but the platform disabled custom domains.
    """
    if domain.status != OrganizationDomain.Status.PENDING or not domain.cloudflare_hostname_id:
        return domain
    hosted = cloudflare.get_custom_hostname(domain.cloudflare_hostname_id)
    domain.verification_records = _dns_records(domain.hostname, hosted)
    domain.save(update_fields=["verification_records", "updated_at"])
    ssl_status = hosted.ssl.status if hosted.ssl else None
    if hosted.status == "active" and ssl_status == "active":
        return activate_domain(domain)
    if hosted.status in FAILED_HOSTNAME_STATUSES or ssl_status in FAILED_SSL_STATUSES:
        return mark_domain_failed(domain)
    return domain


def delete_domain(domain: OrganizationDomain) -> OrganizationDomain:
    """Delete the domain's Cloudflare custom hostname (a 404 counts as already gone) and mark it REMOVED.

    Raises:
        CloudflareAPIError: Cloudflare failed with anything other than "not found".
    """
    if domain.cloudflare_hostname_id and cloudflare.is_configured():
        try:
            cloudflare.delete_custom_hostname(domain.cloudflare_hostname_id)
        except CloudflareAPIError as exc:
            if exc.status_code != 404:
                raise
    return remove_domain(domain)
