from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import check_blacklist, create_token
from accounts.models import RevelUser
from accounts.service.account import token_to_payload
from common.models import SiteSettings
from events import models, schema, tasks
from events.exceptions import AlreadyMemberError, PendingMembershipRequestExistsError
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    OrganizationToken,
    PermissionsSchema,
)
from events.models.organization import _get_default_permissions
from notifications.enums import NotificationType
from notifications.signals import notification_requested


def _create_and_send_contact_email_verification(
    organization: Organization,
    email: str,
    user: RevelUser,
) -> str:
    """Create verification token and send verification email.

    Args:
        organization: The organization
        email: The email to verify
        user: The user associated with the verification

    Returns:
        The verification token
    """
    verification_payload = schema.VerifyOrganizationContactEmailJWTPayloadSchema(
        organization_id=organization.id,
        user_id=user.id,
        email=email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    # Send verification email
    def send_verification_email() -> None:
        tasks.send_organization_contact_email_verification.delay(
            email=email,
            token=token,
            organization_name=organization.name,
            organization_slug=organization.slug,
        )

    transaction.on_commit(send_verification_email)
    return token


@transaction.atomic
def create_organization(
    owner: RevelUser,
    name: str,
    contact_email: str,
    description: str | None = None,
    city_id: int | None = None,
    address: str | None = None,
) -> Organization:
    """Create a new organization.

    Args:
        owner: The user who will own the organization
        name: The name of the organization
        contact_email: The contact email for the organization
        description: Optional description for the organization
        city_id: Optional city id for the organization
        address: Optional address for the organization

    Returns:
        The created Organization instance

    Raises:
        HttpError: If the user already owns an organization
    """
    # Check if user already owns an organization
    if Organization.objects.filter(owner=owner).exists():
        raise HttpError(
            400, str(_("You already own an organization. Only one organization per user as owner is allowed."))
        )

    # Check if contact email matches user email and is verified
    contact_email_verified = False
    if contact_email.lower() == owner.email.lower() and owner.email_verified:
        contact_email_verified = True

    # Create the organization
    organization = Organization.objects.create(
        name=name,
        owner=owner,
        description=description or "",
        contact_email=contact_email,
        contact_email_verified=contact_email_verified,
        visibility=Organization.Visibility.STAFF_ONLY,  # Default to staff only
        city_id=city_id,
        address=address,
    )

    # Send verification email if contact email is not auto-verified
    if not contact_email_verified:
        _create_and_send_contact_email_verification(organization, contact_email, owner)

    return organization


def create_membership_request(
    organization: Organization, user: RevelUser, message: str | None = None
) -> OrganizationMembershipRequest:
    """Create a membership request."""
    if not organization.accept_membership_requests:
        raise HttpError(400, str(_("The organization does not accept new members.")))

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


def create_organization_token(
    *,
    organization: Organization,
    issuer: RevelUser,
    duration: timedelta | int = 60,
    grants_membership: bool = True,
    membership_tier: MembershipTier | None = None,
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
        membership_tier=membership_tier,
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

    if organization_token.grants_staff_status:
        _, created = OrganizationStaff.objects.get_or_create(organization=organization_token.organization, user=user)
    elif organization_token.grants_membership:
        # Create member with tier if specified
        defaults = {}
        if organization_token.membership_tier:
            defaults["tier"] = organization_token.membership_tier

        _, created = OrganizationMember.objects.get_or_create(
            organization=organization_token.organization, user=user, defaults=defaults
        )
    else:
        return None

    if not created:
        return None
    OrganizationToken.objects.filter(pk=token).update(uses=F("uses") + 1)
    return organization_token.organization


def add_member(organization: Organization, user: RevelUser) -> OrganizationMember:
    """Add a member to an organization."""
    if OrganizationMember.objects.filter(organization=organization, user=user).exists():
        raise AlreadyMemberError(str(_("User is already a member of this organization.")))
    return OrganizationMember.objects.create(organization=organization, user=user)


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


@transaction.atomic
def update_contact_email(organization: Organization, new_email: str, requester: RevelUser) -> str:
    """Update organization contact email and send verification.

    Args:
        organization: The organization to update
        new_email: The new contact email address
        requester: The user requesting the change

    Returns:
        The verification token to be sent via email

    Raises:
        HttpError: If the email is the same as current one
    """
    # Check if email is different from current
    if organization.contact_email and organization.contact_email.lower() == new_email.lower():
        raise HttpError(400, str(_("This is already the contact email for this organization.")))

    # Check if new email matches requester's verified email
    if new_email.lower() == requester.email.lower() and requester.email_verified:
        # Automatically verify since it matches the user's verified email
        organization.contact_email = new_email
        organization.contact_email_verified = True
        organization.save(update_fields=["contact_email", "contact_email_verified"])
        return ""  # No token needed

    # Update the email but mark as unverified
    organization.contact_email = new_email
    organization.contact_email_verified = False
    organization.save(update_fields=["contact_email", "contact_email_verified"])

    # Create verification token and send email
    return _create_and_send_contact_email_verification(organization, new_email, requester)


@transaction.atomic
def verify_contact_email(token: str) -> Organization:
    """Verify an organization's contact email.

    Args:
        token: The verification token

    Returns:
        The organization with verified contact email

    Raises:
        HttpError: If token is invalid or organization not found
    """
    payload = token_to_payload(token, schema.VerifyOrganizationContactEmailJWTPayloadSchema)
    check_blacklist(payload.jti)

    organization = Organization.objects.filter(id=payload.organization_id).first()
    if not organization:
        raise HttpError(400, str(_("Organization not found.")))

    # Check that the email in the token matches the current contact email
    if not organization.contact_email or organization.contact_email.lower() != payload.email.lower():
        raise HttpError(400, str(_("This verification link is for a different email address.")))

    # Mark as verified
    blacklist_token(token)
    organization.contact_email_verified = True
    organization.save(update_fields=["contact_email_verified"])

    return organization
