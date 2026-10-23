"""Custom-domain lifecycle (white-label, #1152).

The only writer of ``Organization.custom_domain``, the denormalized hostname of an
organization's ACTIVE domain. Every transition runs in a transaction holding the
organization's row lock, keeps that column in step with the domain status, and drops the
tenant-resolution cache entry once the transaction commits.
"""

import typing as t

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from events.exceptions import CustomDomainNotEnabledError
from events.models import Organization, OrganizationDomain
from events.service.tenant_service import invalidate_tenant_cache

CUSTOM_DOMAIN_NOT_ENABLED_MESSAGE = _("Custom domains are not enabled for this organization.")


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
