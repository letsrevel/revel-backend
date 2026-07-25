"""Organization membership concerns: membership requests, members, and staff."""

import typing as t
from uuid import UUID

from django.db import transaction
from django.db.models import Max
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

if t.TYPE_CHECKING:
    from events.schema import MembershipTierCreateSchema, MembershipTierUpdateSchema

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
        organization=organization,
        user=user,
        tier__isnull=True,
        status=OrganizationMembershipRequest.Status.PENDING,
    ).exists():
        raise PendingMembershipRequestExistsError

    return OrganizationMembershipRequest.objects.create(organization=organization, user=user, message=message)


@transaction.atomic
def approve_membership_request(
    membership_request: models.OrganizationMembershipRequest,
    decided_by: RevelUser,
    tier: MembershipTier | None = None,
) -> None:
    """Approve a membership application.

    Uses the application's pre-set ``tier`` when present; otherwise requires the
    caller to pass ``tier`` explicitly.

    Behavior:
    - Application carries no plan → COMPLETED + OrganizationMember created at the chosen tier.
    - Application carries a plan → APPROVED (Phase 2 will trigger Stripe sub creation
      from a separate /pay endpoint). Phase 1 currently rejects plan-bearing applications
      at /apply, so this branch will rarely fire until Phase 2.
    """
    effective_tier = membership_request.tier or tier
    if effective_tier is None:
        raise HttpError(400, str(_("A tier must be specified to approve this application.")))
    if effective_tier.organization_id != membership_request.organization_id:
        raise HttpError(400, str(_("Tier must belong to the same organization.")))

    membership_request.decided_by = decided_by

    if membership_request.plan_id is None:
        # Free path: complete now.
        membership_request.status = models.OrganizationMembershipRequest.Status.COMPLETED
        update_fields = ["status", "decided_by"]
        if membership_request.tier_id is None:
            membership_request.tier = effective_tier
            update_fields.append("tier")
        membership_request.save(update_fields=update_fields)

        member, created = models.OrganizationMember.objects.update_or_create(
            organization=membership_request.organization,
            user=membership_request.user,
            defaults={"tier": effective_tier, "status": OrganizationMember.MembershipStatus.ACTIVE},
        )
        member.full_clean()
    else:
        # Paid path: mark APPROVED, awaiting member's /pay (Phase 2).
        membership_request.status = models.OrganizationMembershipRequest.Status.APPROVED
        update_fields = ["status", "decided_by"]
        if membership_request.tier_id is None:
            membership_request.tier = effective_tier
            update_fields.append("tier")
        membership_request.save(update_fields=update_fields)
        created = False  # don't fire MEMBERSHIP_GRANTED on the paid branch yet

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

    if created or membership_request.plan_id is None:
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


def validate_membership_questionnaire(organization: Organization, questionnaire_id: UUID) -> None:
    """Ensure ``questionnaire_id`` names a MEMBERSHIP questionnaire owned by ``organization``.

    Mirrors the model-level ``clean()`` rules (org-scoped, MEMBERSHIP type) but runs before
    ``save()`` so callers get a clean 400 for a cross-org, wrong-type, or non-existent id —
    the model ``clean()`` would 500 on the last case when it dereferences the FK.

    Args:
        organization: The organization the questionnaire must belong to.
        questionnaire_id: The candidate ``OrganizationQuestionnaire`` id.

    Raises:
        HttpError 400: If no matching MEMBERSHIP questionnaire exists for the organization.
    """
    exists = models.OrganizationQuestionnaire.objects.filter(
        pk=questionnaire_id,
        organization=organization,
        questionnaire_type=models.OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
    ).exists()
    if not exists:
        raise HttpError(
            400,
            str(_("The membership questionnaire must belong to this organization and be of type MEMBERSHIP.")),
        )


@transaction.atomic
def create_membership_tier(organization: Organization, payload: "MembershipTierCreateSchema") -> MembershipTier:
    """Create a membership tier, appending it at the bottom of the organization's ordering.

    Model ordering is ["organization", "display_order", "name"], so a new tier left at
    display_order 0 would sort to the top (see #514). We lock the organization row first so
    concurrent creates for the same org serialize and cannot read the same max; ATOMIC_REQUESTS
    keeps the lock until the request commits, after the tier is persisted below.

    Args:
        organization: The organization to create the tier for.
        payload: The validated ``MembershipTierCreateSchema`` payload.

    Returns:
        The created ``MembershipTier``, appended after any existing tiers.
    """
    if payload.membership_questionnaire_id:
        validate_membership_questionnaire(organization, payload.membership_questionnaire_id)
    Organization.objects.select_for_update().filter(pk=organization.pk).first()
    current_max = MembershipTier.objects.filter(organization=organization).aggregate(m=Max("display_order"))["m"]
    display_order = 0 if current_max is None else current_max + 1
    return MembershipTier.objects.create(organization=organization, display_order=display_order, **payload.model_dump())


@transaction.atomic
def update_membership_tier(tier: MembershipTier, payload: "MembershipTierUpdateSchema") -> MembershipTier:
    """Update a membership tier, validating any membership-questionnaire override first.

    The tier-level questionnaire (when set) must belong to the tier's organization and be of
    type MEMBERSHIP; a NULL override clears it (inherit the org default). Delegates the field
    write to ``update_db_instance`` (locked, ``exclude_unset`` so tri-state fields keep their
    "not provided vs explicit null" distinction).

    Args:
        tier: The tier to update.
        payload: The validated ``MembershipTierUpdateSchema`` payload.

    Returns:
        The updated ``MembershipTier``.

    Raises:
        HttpError 400: If ``membership_questionnaire_id`` is not a MEMBERSHIP questionnaire for the org.
    """
    from events.service import update_db_instance

    data = payload.model_dump(exclude_unset=True)
    if data.get("membership_questionnaire_id"):
        validate_membership_questionnaire(tier.organization, data["membership_questionnaire_id"])
    return update_db_instance(tier, payload)


@transaction.atomic
def reorder_membership_tiers(organization: Organization, tier_ids: list[UUID]) -> None:
    """Reorder an organization's membership tiers by setting display_order from list position.

    Args:
        organization: The organization whose tiers are being reordered.
        tier_ids: Ordered list of tier UUIDs representing the desired display order.

    Raises:
        HttpError 400: If tier_ids don't match the organization's tiers exactly.
    """
    existing_ids = set(MembershipTier.objects.filter(organization=organization).values_list("id", flat=True))

    if set(tier_ids) != existing_ids:
        raise HttpError(400, str(_("Tier IDs must match all tiers for this organization exactly.")))

    tiers_to_update = [MembershipTier(pk=tier_id, display_order=index) for index, tier_id in enumerate(tier_ids)]
    MembershipTier.objects.bulk_update(tiers_to_update, ["display_order"])


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
