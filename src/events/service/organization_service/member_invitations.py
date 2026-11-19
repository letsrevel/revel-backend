"""Personal membership invitations: staff invite an account, the account accepts through the claim flow.

An invitation is an ``OrganizationToken`` bound to one ``invitee``. Nothing is created for the invitee
until they claim it (``tokens.claim_invitation``), so staff never grant access on an account's behalf.
"""

import typing as t
from datetime import datetime, timedelta

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
MEMBER_INVITATION_RESEND_COOLDOWN = timedelta(hours=24)


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


def _needs_new_notification(previous: OrganizationToken, now: datetime) -> bool:
    """Whether re-inviting over ``previous`` should notify the invitee again (at most once per cooldown).

    Only the last notification time counts, not whether the previous invitation expired, was used or was
    withdrawn: a natural 14-day expiry is always past the cooldown, and a withdrawal must not reset it.
    """
    notified_at = previous.invitation_notified_at
    return notified_at is None or now - notified_at > MEMBER_INVITATION_RESEND_COOLDOWN


@transaction.atomic
def invite_member(
    organization: Organization, invitee: RevelUser, tier: MembershipTier, issuer: RevelUser
) -> OrganizationToken:
    """Create, or refresh, the single personal membership invitation for ``invitee``.

    Re-inviting resets the same row (new tier, expiry and use count). The invitee is notified for a new row or
    when the last notification is older than ``MEMBER_INVITATION_RESEND_COOLDOWN``.

    Raises:
        AlreadyMemberError: An access row exists for the account in any status (including banned).
        HttpError: 400 when the account is hard-blacklisted by the organization.
    """
    if OrganizationMember.objects.filter(organization=organization, user=invitee).exists():
        raise AlreadyMemberError(str(_("User is already a member of this organization.")))
    if blacklist_service.check_user_hard_blacklisted(invitee, organization):
        raise HttpError(400, str(_("This account cannot be invited to the organization.")))

    now = timezone.now()
    # Lock the previous row (if any) so the notify decision sees its pre-refresh state.
    previous = OrganizationToken.objects.select_for_update().filter(organization=organization, invitee=invitee).first()
    notify = previous is None or _needs_new_notification(previous, now)
    defaults: dict[str, t.Any] = {
        "grants_membership": True,
        "grants_staff_status": False,
        "membership_tier": tier,
        "max_uses": 1,
        "uses": 0,
        "expires_at": now + MEMBER_INVITATION_TTL,
        "issuer": issuer,
        "name": None,
    }
    if notify:
        defaults["invitation_notified_at"] = now
    token, created = update_or_create_with_race_protection(
        OrganizationToken, {"organization": organization, "invitee": invitee}, defaults
    )
    # A concurrent first creation that lost the race leaves the notification to the winner.
    if notify and (previous is not None or created):
        transaction.on_commit(lambda: _dispatch_invitation_received(token, invitee, tier))
    return token


def list_pending_invitations(organization: Organization) -> QuerySet[OrganizationToken]:
    """Personal invitations of ``organization`` that can still be accepted.

    An invitation ends when the invitee becomes a member by any route (``events.signals`` expires it).
    """
    return OrganizationToken.objects.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
        organization=organization,
        invitee__isnull=False,
        uses__lt=F("max_uses"),
    ).select_related("invitee", "membership_tier")


def revoke_invitation(organization: Organization, invitation_id: str) -> None:
    """Withdraw one of the organization's pending personal invitations (404 when it is not one).

    The row is expired rather than deleted so its last notification time keeps the resend cooldown.
    """
    invitation = get_object_or_404(list_pending_invitations(organization), pk=invitation_id)
    invitation.expires_at = timezone.now()
    invitation.save(update_fields=["expires_at", "updated_at"])


def has_relationship(organization: Organization, user: RevelUser) -> bool:
    """Whether ``user`` already has any standing with ``organization``.

    Owner, member (any status), staff, membership applicant (any status) or register record holder.
    """
    if organization.owner_id == user.pk:
        return True
    return (
        OrganizationMember.objects.filter(organization=organization, user=user).exists()
        or OrganizationStaff.objects.filter(organization=organization, user=user).exists()
        or OrganizationMembershipRequest.objects.filter(organization=organization, user=user).exists()
        or MembershipRecord.objects.filter(organization=organization, user=user).exists()
    )


def require_relationship(organization: Organization, user: RevelUser) -> None:
    """Raise ``InvitationRequiredError`` unless ``user`` already has standing with ``organization``."""
    if not has_relationship(organization, user):
        raise InvitationRequiredError
