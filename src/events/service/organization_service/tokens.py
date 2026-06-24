"""Organization token concerns: token CRUD and invitation claiming."""

import typing as t
from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import RevelUser
from events import schema
from events.exceptions import (
    OrganizationTokenGrantInvariantError,
    OrganizationTokenMembershipTierRequiredError,
    OrganizationTokenStaffGrantForbidden,
)
from events.models import (
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    OrganizationToken,
)
from events.service import blacklist_service

# Canned messages for the organization-token guard exceptions. Co-located with
# the service that raises them so the per-app exception handlers (and any caller)
# render an identical, translatable ``{"detail": ...}`` body.
STAFF_GRANT_FORBIDDEN_MESSAGE = _("Only the organization owner can manage staff-granting tokens.")
GRANT_INVARIANT_MESSAGE = _("At least one of grants_membership or grants_staff_status must be True.")
MEMBERSHIP_TIER_REQUIRED_MESSAGE = _("membership_tier_id is required when grants_membership is True.")


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
    """Create an organization token for sharing invitation links.

    Args:
        organization: The organization the token belongs to.
        issuer: The user creating the token.
        duration: Token validity duration in minutes (or timedelta).
        grants_membership: Whether the token grants membership.
        membership_tier: The membership tier to assign (required if grants_membership is True).
        grants_staff_status: Whether the token grants staff status.
        name: Display name for the token.
        max_uses: Maximum number of uses (0 = unlimited).

    Returns:
        The created OrganizationToken.

    Raises:
        ValueError: If both grants_membership and grants_staff_status are False.
        ValueError: If grants_membership is True and membership_tier is None.
    """
    if not grants_membership and not grants_staff_status:
        raise ValueError("At least one of grants_membership or grants_staff_status must be True")
    if grants_membership and membership_tier is None:
        raise ValueError("membership_tier is required when grants_membership is True")
    if isinstance(duration, int):
        duration = timedelta(minutes=duration)
    expires_at = timezone.now() + duration if duration else None
    return OrganizationToken.objects.create(
        name=name,
        issuer=issuer,
        organization=organization,
        expires_at=expires_at,
        max_uses=max_uses,
        grants_membership=grants_membership,
        grants_staff_status=grants_staff_status,
        membership_tier=membership_tier,
    )


def _resolve_membership_tier(organization: Organization, tier_id: UUID | None) -> MembershipTier | None:
    """Resolve a ``MembershipTier`` belonging to ``organization`` by id."""
    if tier_id is None:
        return None
    return get_object_or_404(MembershipTier, pk=tier_id, organization=organization)


def create_organization_token_from_payload(
    *,
    organization: Organization,
    requested_by: RevelUser,
    payload: schema.OrganizationTokenCreateSchema,
) -> OrganizationToken:
    """Create an organization token from an API payload.

    Encapsulates the controller-side work that used to live in
    ``create_organization_token`` view: owner-only privilege guard for
    staff-granting tokens, membership-tier resolution, and dispatch to the
    primitive ``create_organization_token`` constructor.

    Args:
        organization: The organization the token belongs to.
        requested_by: The authenticated user issuing the create request.
        payload: Validated create-schema from the API.

    Returns:
        The created ``OrganizationToken``.

    Raises:
        OrganizationTokenStaffGrantForbidden: When a non-owner tries to create
            a staff-granting token.
    """
    if payload.grants_staff_status and organization.owner_id != requested_by.pk:
        raise OrganizationTokenStaffGrantForbidden

    data = payload.model_dump(exclude_unset=True)
    tier_id = data.pop("membership_tier_id", None)
    membership_tier = _resolve_membership_tier(organization, tier_id)

    return create_organization_token(
        organization=organization,
        issuer=requested_by,
        membership_tier=membership_tier,
        **data,
    )


@transaction.atomic
def update_organization_token(
    token: OrganizationToken,
    *,
    requested_by: RevelUser,
    payload: schema.OrganizationTokenUpdateSchema,
) -> OrganizationToken:
    """Update an organization token from an API payload.

    Enforces the cross-field invariant (``grants_membership`` or
    ``grants_staff_status`` must remain ``True``) and the owner-only guard
    for any update that touches a staff-granting token (either the existing
    token already grants staff, or the update would grant staff). Resolves
    ``membership_tier_id`` to the corresponding ``MembershipTier`` and
    persists changed fields only.

    Args:
        token: The token being updated.
        requested_by: The authenticated user issuing the update request.
        payload: Validated update-schema from the API.

    Returns:
        The refreshed ``OrganizationToken``.

    Raises:
        OrganizationTokenGrantInvariantError: When the resulting state would
            disable both ``grants_membership`` and ``grants_staff_status``.
        OrganizationTokenMembershipTierRequiredError: When the resulting state
            would have ``grants_membership=True`` but no ``membership_tier``.
        OrganizationTokenStaffGrantForbidden: When the requester is not the
            organization owner but the token grants (or would grant) staff
            status.
    """
    data = payload.model_dump(exclude_unset=True)

    resulting_grants_staff = data.get("grants_staff_status", token.grants_staff_status)
    resulting_grants_membership = data.get("grants_membership", token.grants_membership)
    if not resulting_grants_membership and not resulting_grants_staff:
        raise OrganizationTokenGrantInvariantError

    touches_staff_grant = token.grants_staff_status or resulting_grants_staff
    if touches_staff_grant and token.organization.owner_id != requested_by.pk:
        raise OrganizationTokenStaffGrantForbidden

    if "membership_tier_id" in data:
        tier_id = data.pop("membership_tier_id")
        data["membership_tier"] = _resolve_membership_tier(token.organization, tier_id)

    # Resulting tier state: explicit value in this update, else the token's current tier.
    resulting_tier = data["membership_tier"] if "membership_tier" in data else token.membership_tier
    if resulting_grants_membership and resulting_tier is None:
        raise OrganizationTokenMembershipTierRequiredError

    if not data:
        return token

    for field, value in data.items():
        setattr(token, field, value)
    token.save(update_fields=list(data.keys()))
    return token


def delete_organization_token(
    token: OrganizationToken,
    *,
    requested_by: RevelUser,
) -> None:
    """Delete an organization token, enforcing the owner-only guard.

    Args:
        token: The token to delete.
        requested_by: The authenticated user issuing the delete request.

    Raises:
        OrganizationTokenStaffGrantForbidden: When the requester is not the
            organization owner but the token grants staff status.
    """
    if token.grants_staff_status and token.organization.owner_id != requested_by.pk:
        raise OrganizationTokenStaffGrantForbidden
    token.delete()


class OrgTokenRejection(t.NamedTuple):
    """Why an organization token was rejected, plus the org it belongs to."""

    reason: t.Literal["expired", "used_up"]
    organization_id: UUID


def get_organization_token(token: str) -> OrganizationToken | None:
    """Retrieve an OrganizationToken by its ID.

    Returns the token only if it is still valid: not expired and not
    exhausted (``uses < max_uses``, or ``max_uses == 0`` for unlimited).
    """
    return (
        OrganizationToken.objects.select_related("organization")
        .filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
            Q(max_uses=0) | Q(uses__lt=F("max_uses")),
            pk=token,
        )
        .first()
    )


def get_org_token_rejection_reason(token: str) -> OrgTokenRejection | None:
    """Diagnose why an organization token is no longer valid.

    Called only after get_organization_token() returned None to distinguish
    "token doesn't exist" from "token expired / used up".
    """
    org_token = (
        OrganizationToken.objects.only("expires_at", "uses", "max_uses", "organization_id").filter(pk=token).first()
    )
    if org_token is None:
        return None
    if org_token.expires_at and org_token.expires_at <= timezone.now():
        return OrgTokenRejection(reason="expired", organization_id=org_token.organization_id)
    if org_token.max_uses and org_token.uses >= org_token.max_uses:
        return OrgTokenRejection(reason="used_up", organization_id=org_token.organization_id)
    return None


@transaction.atomic
def claim_invitation(user: RevelUser, token: str) -> Organization | None:
    """Claim an invitation given a Token."""
    organization_token = (
        OrganizationToken.objects.select_for_update()
        .select_related("organization")
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()), pk=token)
        .first()
    )
    if organization_token is None:
        return None
    if organization_token.max_uses and organization_token.uses >= organization_token.max_uses:
        return None

    # A hard-blacklisted (banned) user must not be able to claim their way back in.
    # The membership path is blocked implicitly by colliding with the BANNED member row,
    # but OrganizationStaff has no status field, so a banned ex-staffer could otherwise
    # re-claim a grants_staff_status token and regain staff (bypassing BlacklistGate).
    # Guard both grant paths explicitly, mirroring request_membership.
    if blacklist_service.check_user_hard_blacklisted(user, organization_token.organization):
        return None

    # Apply every grant the token carries. A token may grant both staff status and
    # membership (grants_membership defaults to True), so the paths are evaluated
    # independently — not as an either/or — otherwise the membership grant would be
    # silently dropped on a staff-granting token.
    created = False
    if organization_token.grants_staff_status:
        _, staff_created = OrganizationStaff.objects.get_or_create(
            organization=organization_token.organization, user=user
        )
        created = created or staff_created
    if organization_token.grants_membership:
        # Create member with tier if specified
        defaults = {}
        if organization_token.membership_tier:
            defaults["tier"] = organization_token.membership_tier

        _, member_created = OrganizationMember.objects.get_or_create(
            organization=organization_token.organization, user=user, defaults=defaults
        )
        created = created or member_created

    if not created:
        return None
    OrganizationToken.objects.filter(pk=token).update(uses=F("uses") + 1)
    return organization_token.organization
