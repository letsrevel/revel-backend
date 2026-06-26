"""Organization membership concerns: membership requests, members, and staff."""

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.models import SiteSettings
from events import models
from events.exceptions import AlreadyMemberError, PendingMembershipRequestExistsError
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    PermissionsSchema,
)

# Intentional cross-module use of a private helper: the name is pinned by
# events/migrations/0001_initial.py (referenced as a field `default=`), so it
# cannot be renamed without breaking historical migrations.
from events.models.organization import _get_default_permissions
from events.service import blacklist_service
from notifications.enums import NotificationType
from notifications.signals import notification_requested


def create_membership_request(
    organization: Organization, user: RevelUser, message: str | None = None
) -> OrganizationMembershipRequest:
    """Create a membership request.

    Args:
        organization: The organization to request membership for.
        user: The user requesting membership.
        message: Optional message from the user.

    Returns:
        The created OrganizationMembershipRequest instance.

    Raises:
        HttpError: If the organization does not accept requests, or the user is blacklisted.
        AlreadyMemberError: If the user is already a member.
        PendingMembershipRequestExistsError: If a pending request already exists.
    """
    if not organization.accept_membership_requests:
        raise HttpError(400, str(_("The organization does not accept new members.")))

    if blacklist_service.check_user_hard_blacklisted(user, organization):
        raise HttpError(403, str(_("You are not allowed to request membership for this organization.")))

    if models.OrganizationMember.objects.filter(organization=organization, user=user).exists():
        raise AlreadyMemberError

    if OrganizationMembershipRequest.objects.filter(
        organization=organization, user=user, status=OrganizationMembershipRequest.Status.PENDING
    ).exists():
        raise PendingMembershipRequestExistsError

    return OrganizationMembershipRequest.objects.create(organization=organization, user=user, message=message)


@transaction.atomic
def approve_membership_request(
    membership_request: models.OrganizationMembershipRequest, decided_by: RevelUser, tier: MembershipTier
) -> None:
    """Approve a membership request and assign tier.

    Args:
        membership_request: The membership request to approve
        decided_by: The user approving the request
        tier: The membership tier to assign
    """
    membership_request.status = models.OrganizationMembershipRequest.Status.APPROVED
    membership_request.decided_by = decided_by
    membership_request.save(update_fields=["status", "decided_by"])

    # Create or update membership with tier (this will trigger MEMBERSHIP_GRANTED notification via signal)
    # Use update_or_create to ensure clean() method is called for validation
    member, created = models.OrganizationMember.objects.update_or_create(
        organization=membership_request.organization,
        user=membership_request.user,
        defaults={"tier": tier, "status": OrganizationMember.MembershipStatus.ACTIVE},
    )

    # Explicitly call clean to validate tier belongs to same organization
    member.full_clean()

    # Send approval notification
    def send_approval_notification() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        notification_requested.send(
            sender=models.OrganizationMembershipRequest,
            user=membership_request.user,
            notification_type=NotificationType.MEMBERSHIP_REQUEST_APPROVED,
            context={
                "organization_id": str(membership_request.organization_id),
                "organization_name": membership_request.organization.name,
                "frontend_url": f"{frontend_base_url}/org/{membership_request.organization.slug}",
            },
        )

    if created:
        transaction.on_commit(send_approval_notification)


def reject_membership_request(request: models.OrganizationMembershipRequest, decided_by: RevelUser) -> None:
    """Reject a membership request."""
    request.status = models.OrganizationMembershipRequest.Status.REJECTED
    request.decided_by = decided_by
    request.save(update_fields=["status", "decided_by"])

    # Send rejection notification
    def send_rejection_notification() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        notification_requested.send(
            sender=models.OrganizationMembershipRequest,
            user=request.user,
            notification_type=NotificationType.MEMBERSHIP_REQUEST_REJECTED,
            context={
                "organization_id": str(request.organization_id),
                "organization_name": request.organization.name,
                "frontend_url": f"{frontend_base_url}/organizations",
            },
        )

    transaction.on_commit(send_rejection_notification)


def add_member(organization: Organization, user: RevelUser, tier: MembershipTier) -> OrganizationMember:
    """Add a member to an organization.

    Args:
        organization: The organization to add the member to.
        user: The user to add as a member.
        tier: The membership tier to assign to the member.

    Returns:
        The created OrganizationMember instance.

    Raises:
        AlreadyMemberError: If the user is already a member of the organization.
    """
    if OrganizationMember.objects.filter(organization=organization, user=user).exists():
        raise AlreadyMemberError(str(_("User is already a member of this organization.")))
    return OrganizationMember.objects.create(organization=organization, user=user, tier=tier)


def remove_member(organization: Organization, user: RevelUser) -> None:
    """Remove a member from an organization."""
    member = get_object_or_404(OrganizationMember, organization=organization, user=user)
    member.delete()


def update_member(
    member: OrganizationMember,
    *,
    status: OrganizationMember.MembershipStatus | None = None,
    tier: MembershipTier | None = None,
    clear_tier: bool = False,
) -> OrganizationMember:
    """Update a member's status and/or tier.

    Args:
        member: The OrganizationMember instance to update
        status: New membership status (if provided)
        tier: New membership tier (if provided)
        clear_tier: If True, sets tier to None

    Returns:
        Updated OrganizationMember instance
    """
    updated_fields = []

    if status is not None:
        member.status = status
        updated_fields.append("status")

    if clear_tier:
        member.tier = None
        updated_fields.append("tier")
    elif tier is not None:
        member.tier = tier
        updated_fields.append("tier")

    if updated_fields:
        member.save(update_fields=updated_fields)

    return member


def add_staff(
    organization: Organization, user: RevelUser, permissions: PermissionsSchema | None = None
) -> OrganizationStaff:
    """Add a staff member to an organization."""
    if OrganizationStaff.objects.filter(organization=organization, user=user).exists():
        raise AlreadyMemberError(str(_("User is already a staff member of this organization.")))

    permission_data = permissions.model_dump(mode="json") if permissions else _get_default_permissions()

    return OrganizationStaff.objects.create(organization=organization, user=user, permissions=permission_data)


def remove_staff(organization: Organization, user: RevelUser) -> None:
    """Remove a staff member from an organization."""
    staff = get_object_or_404(OrganizationStaff, organization=organization, user=user)
    staff.delete()


def update_staff_permissions(staff_member: OrganizationStaff, permissions: PermissionsSchema) -> OrganizationStaff:
    """Update the permissions for a staff member."""
    staff_member.permissions = permissions.model_dump(mode="json")
    staff_member.save()
    return staff_member
