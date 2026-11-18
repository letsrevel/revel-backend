"""Personal membership invitations: staff invite an account, the account accepts through the claim flow.

An invitation is an ``OrganizationToken`` bound to one ``invitee``. Nothing is created for the invitee
until they claim it (``tokens.claim_invitation``), so staff never grant access on an account's behalf.
"""

import typing as t
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.utils import update_or_create_with_race_protection
from events.exceptions import AlreadyMemberError, InvitationRequiredError
from events.models import (
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    OrganizationToken,
)
from events.service import blacklist_service
from events.utils import format_organization_datetime
from events.utils.frontend_urls import frontend_base_url_for
from notifications.enums import NotificationType
from notifications.membership_context_schemas import MembershipInvitationReceivedContext
from notifications.signals import notification_requested

MEMBER_INVITATION_TTL = timedelta(days=14)


def _dispatch_invitation_received(token: OrganizationToken, invitee: RevelUser, tier: MembershipTier) -> None:
    """Tell the invitee they were invited (fires on commit; the handler never raises)."""
    organization = token.organization
    assert token.expires_at is not None  # noqa: S101 - invitations always carry an expiry
    context: MembershipInvitationReceivedContext = {
        "organization_id": str(organization.pk),
        "organization_name": organization.name,
        "tier_name": tier.name,
        "expires_at": format_organization_datetime(token.expires_at, organization),
        "accept_url": f"{frontend_base_url_for(organization)}/join/org/{token.pk}",
    }
    notification_requested.send(
        sender=OrganizationToken,
        user=invitee,
        notification_type=NotificationType.MEMBERSHIP_INVITATION_RECEIVED,
        context=context,
    )


@transaction.atomic
def invite_member(
    organization: Organization, invitee: RevelUser, tier: MembershipTier, issuer: RevelUser
) -> OrganizationToken:
    """Create, or refresh, the single personal membership invitation for ``invitee``.

    Re-inviting resets the same row (new tier, expiry and use count) and notifies again.

    Raises:
        AlreadyMemberError: An access row exists for the account in any status (including banned).
        HttpError: 400 when the account is hard-blacklisted by the organization.
    """
    if OrganizationMember.objects.filter(organization=organization, user=invitee).exists():
        raise AlreadyMemberError(str(_("User is already a member of this organization.")))
    if blacklist_service.check_user_hard_blacklisted(invitee, organization):
        raise HttpError(400, str(_("This account cannot be invited to the organization.")))

    token, _created = update_or_create_with_race_protection(
        OrganizationToken,
        {"organization": organization, "invitee": invitee},
        {
            "grants_membership": True,
            "grants_staff_status": False,
            "membership_tier": tier,
            "max_uses": 1,
            "uses": 0,
            "expires_at": timezone.now() + MEMBER_INVITATION_TTL,
            "issuer": issuer,
            "name": None,
        },
    )
    transaction.on_commit(lambda: _dispatch_invitation_received(token, invitee, tier))
    return token


def list_pending_invitations(organization: Organization) -> QuerySet[OrganizationToken]:
    """Personal invitations of ``organization`` that can still be accepted."""
    return OrganizationToken.objects.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
        organization=organization,
        invitee__isnull=False,
        uses__lt=F("max_uses"),
    ).select_related("invitee", "membership_tier")


def revoke_invitation(organization: Organization, invitation_id: str) -> None:
    """Delete one of the organization's personal invitations (404 when it is not one)."""
    get_object_or_404(
        OrganizationToken.objects.filter(invitee__isnull=False), pk=invitation_id, organization=organization
    ).delete()


def has_relationship(organization: Organization, user: RevelUser) -> bool:
    """Whether ``user`` already has any standing with ``organization``.

    Owner, member (any status), staff, membership applicant (any status) or register record holder.
    """
    if organization.owner_id == user.pk:
        return True
    lookup: dict[str, t.Any] = {"organization": organization, "user": user}
    return (
        OrganizationMember.objects.filter(**lookup).exists()
        or OrganizationStaff.objects.filter(**lookup).exists()
        or OrganizationMembershipRequest.objects.filter(**lookup).exists()
        or MembershipRecord.objects.filter(**lookup).exists()
    )


def require_relationship(organization: Organization, user: RevelUser) -> None:
    """Raise ``InvitationRequiredError`` unless ``user`` already has standing with ``organization``."""
    if not has_relationship(organization, user):
        raise InvitationRequiredError
