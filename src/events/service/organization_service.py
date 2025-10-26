from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events import models
from events.exceptions import AlreadyMemberError, PendingMembershipRequestExistsError
from events.models import (
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    OrganizationToken,
    PermissionsSchema,
)
from events.models.organization import _get_default_permissions


def create_membership_request(
    organization: Organization, user: RevelUser, message: str | None = None
) -> OrganizationMembershipRequest:
    """Create a membership request."""
    if not organization.accept_membership_requests:
        raise HttpError(400, "The organization does not accept new members.")

    if models.OrganizationMember.objects.filter(organization=organization, user=user).exists():
        raise AlreadyMemberError

    if OrganizationMembershipRequest.objects.filter(
        organization=organization, user=user, status=OrganizationMembershipRequest.Status.PENDING
    ).exists():
        raise PendingMembershipRequestExistsError

    return OrganizationMembershipRequest.objects.create(organization=organization, user=user, message=message)


@transaction.atomic
def approve_membership_request(membership_request: models.OrganizationMembershipRequest, decided_by: RevelUser) -> None:
    """Approve a membership request."""
    membership_request.status = models.OrganizationMembershipRequest.Status.APPROVED
    membership_request.decided_by = decided_by
    membership_request.save(update_fields=["status", "decided_by"])
    models.OrganizationMember.objects.create(organization=membership_request.organization, user=membership_request.user)


def reject_membership_request(request: models.OrganizationMembershipRequest, decided_by: RevelUser) -> None:
    """Reject a membership request."""
    request.status = models.OrganizationMembershipRequest.Status.REJECTED
    request.decided_by = decided_by
    request.save(update_fields=["status", "decided_by"])


def create_organization_token(
    *,
    organization: Organization,
    issuer: RevelUser,
    duration: timedelta | int = 60,
    grants_membership: bool = True,
    grants_staff_status: bool = False,
    name: str | None = None,
    max_uses: int = 0,
) -> OrganizationToken:
    """Get a temporary JWT.

    This will need to be used by a user in combination with their OTP code to obtain a valid JWT.
    """
    duration = timedelta(minutes=duration) if isinstance(duration, int) else duration
    return OrganizationToken.objects.create(
        name=name,
        issuer=issuer,
        organization=organization,
        expires_at=timezone.now() + duration,
        max_uses=max_uses,
        grants_membership=grants_membership,
        grants_staff_status=grants_staff_status,
    )


def get_organization_token(token: str) -> OrganizationToken | None:
    """Retrieves an EventToken from a JWT."""
    return (
        OrganizationToken.objects.select_related("organization")
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()), pk=token)
        .first()
    )


@transaction.atomic
def claim_invitation(user: RevelUser, token: str) -> Organization | None:
    """Claim an invitation given a Token."""
    organization_token = get_organization_token(token)
    if organization_token is None:
        return None
    if organization_token.max_uses and organization_token.uses >= organization_token.max_uses:
        return None
    klass: type[OrganizationStaff] | type[OrganizationMember]
    if organization_token.grants_staff_status:
        klass = OrganizationStaff
    elif organization_token.grants_membership:
        klass = OrganizationMember
    else:
        return None
    _, created = klass.objects.get_or_create(organization=organization_token.organization, user=user)
    if not created:
        return None
    OrganizationToken.objects.filter(pk=token).update(uses=F("uses") + 1)
    return organization_token.organization


def add_member(organization: Organization, user: RevelUser) -> OrganizationMember:
    """Add a member to an organization."""
    if OrganizationMember.objects.filter(organization=organization, user=user).exists():
        raise AlreadyMemberError("User is already a member of this organization.")
    return OrganizationMember.objects.create(organization=organization, user=user)


def remove_member(organization: Organization, user: RevelUser) -> None:
    """Remove a member from an organization."""
    member = get_object_or_404(OrganizationMember, organization=organization, user=user)
    member.delete()


def add_staff(
    organization: Organization, user: RevelUser, permissions: PermissionsSchema | None = None
) -> OrganizationStaff:
    """Add a staff member to an organization."""
    if OrganizationStaff.objects.filter(organization=organization, user=user).exists():
        raise AlreadyMemberError("User is already a staff member of this organization.")

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
