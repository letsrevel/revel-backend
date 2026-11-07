"""Linking account-less register records to accounts (#1319).

A record without a user grants nothing. Linking sets ``MembershipRecord.user`` and then makes the access
row (``OrganizationMember``) agree; the register mirror in :mod:`events.service.member_register_service`
runs from the access row's ``post_save`` signal, so this module never imports it.

Two entry points reach :func:`link_record`: a staff-sent invitation link (:func:`claim_invite`, any account)
and the verified-email match (:func:`auto_link_records_for_user`).
"""

import enum
import secrets
from dataclasses import dataclass
from uuid import UUID

import structlog
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from events.models import MembershipRecord, Organization, OrganizationMember
from events.service import blacklist_service
from events.utils.frontend_urls import frontend_base_url_for
from notifications.context_schemas import StaffMemberRecordAutoLinkedContext
from notifications.enums import NotificationType
from notifications.service.notification_helpers import notify_org_staff
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)

Status = OrganizationMember.MembershipStatus


class LinkOutcome(enum.StrEnum):
    """What a link attempt did."""

    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    DUPLICATE = "duplicate"
    BANNED = "banned"
    NOT_STARTED = "not_started"


@dataclass(frozen=True)
class LinkResult:
    """Outcome of linking one record; ``member`` is the claimant's access row after a LINKED outcome."""

    outcome: LinkOutcome
    record: MembershipRecord
    member: OrganizationMember | None = None


def notify_membership_granted(member: OrganizationMember) -> None:
    """Tell the member they were granted membership (MEMBERSHIP_GRANTED).

    Shared by the ``post_save`` receiver (new access rows) and by linking, which reactivates a CANCELLED
    row (the signal only fires on create). Call it from ``transaction.on_commit``.
    """
    organization = member.organization
    notification_requested.send(
        sender=OrganizationMember,
        user=member.user,
        notification_type=NotificationType.MEMBERSHIP_GRANTED,
        context={
            "organization_id": str(member.organization_id),
            "organization_name": organization.name,
            "role": "member",
            "action": "granted",
            "frontend_url": f"{frontend_base_url_for(organization)}/org/{organization.slug}",
        },
    )
    logger.info(
        "membership_granted_notification_sent", organization_id=str(member.organization_id), user_id=str(member.user_id)
    )


def _make_access_row_agree(
    record: MembershipRecord, user: RevelUser, member: OrganizationMember | None
) -> OrganizationMember:
    """Bring the claimant's access row in line with the just-linked open STANDARD ``record``."""
    if member is None:
        # The mirror sees the already-linked open record with the same tier and does nothing.
        member, _created = get_or_create_with_race_protection(
            OrganizationMember,
            Q(organization_id=record.organization_id, user=user),
            {"organization_id": record.organization_id, "user": user, "status": Status.ACTIVE, "tier": record.tier},
        )
        return member
    if member.status == Status.CANCELLED:
        member.status = Status.ACTIVE
        member.tier = record.tier
        member.save(update_fields=["status", "tier", "updated_at"])
        transaction.on_commit(lambda: notify_membership_granted(member))
        return member
    # Legacy member without an open record (ACTIVE/PAUSED): keep tier and status, let the mirror reconcile.
    member.save(update_fields=["updated_at"])
    return member


def link_record(record: MembershipRecord, user: RevelUser, *, token: str | None = None) -> LinkResult:
    """Link an account-less ``record`` to ``user``, applying the collision rules in order.

    Locks the claimant, then their access row, then the record (the mirror locks member -> record).
    When ``token`` is given it is re-checked against the locked record, so a link rotated or used
    between the caller's lookup and this lock reads as ALREADY_LINKED.
    """
    with transaction.atomic():
        # NO KEY UPDATE: a plain FOR UPDATE would conflict with the KEY SHARE lock that inserting a record
        # or access row referencing this user takes on it (FK check), deadlocking against concurrent inserts.
        user = RevelUser.objects.select_for_update(no_key=True).get(pk=user.pk)
        member = (
            OrganizationMember.objects.select_for_update(of=("self",))
            .select_related("organization", "tier")
            .filter(organization_id=record.organization_id, user=user)
            .first()
        )
        locked = (
            MembershipRecord.objects.select_for_update(of=("self",))
            .select_related("organization", "tier")
            .get(pk=record.pk)
        )

        if locked.user_id is not None or (token is not None and locked.invite_token != token):
            return LinkResult(LinkOutcome.ALREADY_LINKED, locked)

        open_standard = locked.valid_until is None and locked.kind == MembershipRecord.Kind.STANDARD
        if open_standard:
            if locked.valid_from > timezone.now():
                return LinkResult(LinkOutcome.NOT_STARTED, locked)
            if (member is not None and member.status == Status.BANNED) or blacklist_service.check_user_hard_blacklisted(
                user, locked.organization
            ):
                return LinkResult(LinkOutcome.BANNED, locked)
            has_open_standard = (
                MembershipRecord.objects.open()
                .filter(organization_id=locked.organization_id, user=user, kind=MembershipRecord.Kind.STANDARD)
                .exists()
            )
            if has_open_standard:
                locked.duplicate_claimant = user
                locked.save(update_fields=["duplicate_claimant", "updated_at"])
                return LinkResult(LinkOutcome.DUPLICATE, locked)

        locked.user = user
        locked.invite_token = None
        locked.duplicate_claimant = None
        locked.save(update_fields=["user", "invite_token", "duplicate_claimant", "updated_at"])
        if open_standard:
            member = _make_access_row_agree(locked, user, member)
        else:
            member = None  # closed / TEMPORARY records grant nothing: no access row is touched
        return LinkResult(LinkOutcome.LINKED, locked, member)


def adopt_unlinked_record(member: OrganizationMember) -> MembershipRecord | None:
    """Link the earliest unlinked open STANDARD record that verifiably belongs to the member's account.

    Prevention at the source: called when the register is about to open a *new* STANDARD record for an
    access row. Only a verified exact email match adopts (never guests, never unverified emails); looser
    matches are only flagged for staff (see ``member_register_reconciliation``). The access row already
    exists, so it is left alone: the caller settles tier and evidence. Staff are told, like for B1's auto-link.
    """
    user = member.user
    if user.guest or not user.email_verified or not user.email:
        return None
    record = (
        MembershipRecord.objects.unlinked()
        .open()
        .select_for_update(of=("self",))
        .select_related("organization")
        .filter(organization_id=member.organization_id, kind=MembershipRecord.Kind.STANDARD, email__iexact=user.email)
        .order_by("valid_from", "created_at")
        .first()
    )
    if record is None:
        return None
    record.user = user
    record.invite_token = None
    record.duplicate_claimant = None
    record.save(update_fields=["user", "invite_token", "duplicate_claimant", "updated_at"])
    logger.info("member_register_adopted_at_source", record_id=str(record.pk), user_id=str(user.pk))
    _notify_staff_of_auto_link(record, user)
    return record


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _dispatch_invite(record_id: UUID) -> None:
    from events.tasks.member_register import send_member_register_invite  # lazy: the task imports this module

    # Only the id travels through the broker: the task reads the current token itself.
    transaction.on_commit(lambda: send_member_register_invite.delay(str(record_id)))


def send_invite(record: MembershipRecord) -> MembershipRecord:
    """Send (or resend) the claim link for one record. A resend invalidates the previous link."""
    if record.user_id is not None:
        raise HttpError(400, str(_("This register entry is already linked to an account.")))
    if not record.email:
        raise HttpError(400, str(_("This register entry has no email address to invite.")))
    if record.ended_reason == MembershipRecord.EndedReason.REVOKED:
        raise HttpError(400, str(_("A revoked register entry cannot be invited.")))
    if (
        record.kind == MembershipRecord.Kind.STANDARD
        and record.valid_until is None
        and record.valid_from > timezone.now()
    ):
        raise HttpError(400, str(_("This register entry starts in the future; invite the person once it has started.")))
    record.invite_token = _new_token()
    record.invited_at = timezone.now()
    record.save(update_fields=["invite_token", "invited_at", "updated_at"])
    _dispatch_invite(record.pk)
    return record


def send_bulk_invites(organization: Organization) -> int:
    """Invite every open STANDARD, unlinked, never-invited record that has an email. Returns the count.

    Closed records and already-invited ones are skipped; a resend is per record.
    """
    now = timezone.now()
    with transaction.atomic():
        records = list(
            MembershipRecord.objects.open()
            .unlinked()
            .select_for_update(of=("self",))
            .filter(
                organization=organization,
                kind=MembershipRecord.Kind.STANDARD,
                invited_at__isnull=True,
                valid_from__lte=now,
            )
            .exclude(email="")
        )
        for record in records:
            record.invite_token = _new_token()
            record.invited_at = now
            record.updated_at = now
        MembershipRecord.objects.bulk_update(records, ["invite_token", "invited_at", "updated_at"])
        for record in records:
            _dispatch_invite(record.pk)
    return len(records)


def get_record_for_invite_token(token: str) -> MembershipRecord | None:
    """The unlinked, non-revoked record holding ``token``, if any."""
    return (
        MembershipRecord.objects.unlinked()
        .select_related("organization")
        .filter(invite_token=token)
        .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
        .first()
    )


def claim_invite(token: str, user: RevelUser) -> LinkResult | None:
    """Link the record bound to ``token`` to ``user`` (any account, any email).

    Returns None when the token is unknown or already used, and for guest users (they hold no real
    account, so the claim reads as "not found").
    """
    if user.guest:
        return None
    record = get_record_for_invite_token(token)
    if record is None:
        return None
    result = link_record(record, user, token=token)
    logger.info(
        "member_register_invite_claimed", record_id=str(record.pk), user_id=str(user.pk), outcome=result.outcome
    )
    return None if result.outcome == LinkOutcome.ALREADY_LINKED else result


def auto_link_records_for_user(user_id: UUID) -> list[LinkResult]:
    """Link every unlinked record whose email equals (``iexact``) the user's verified email.

    Never for guests or unverified emails. Staff are told about each successful link.
    """
    user = RevelUser.objects.filter(pk=user_id).first()
    if user is None or user.guest or not user.email_verified or not user.email:
        return []
    records = list(
        MembershipRecord.objects.unlinked()
        .select_related("organization")
        .filter(email__iexact=user.email)
        .order_by("valid_from", "created_at")
    )
    results: list[LinkResult] = []
    for record in records:
        result = link_record(record, user)
        results.append(result)
        logger.info("member_register_auto_link", record_id=str(record.pk), user_id=str(user.pk), outcome=result.outcome)
        if result.outcome == LinkOutcome.LINKED:
            _notify_staff_of_auto_link(result.record, user)
    return results


def _notify_staff_of_auto_link(record: MembershipRecord, user: RevelUser) -> None:
    organization = record.organization
    context: StaffMemberRecordAutoLinkedContext = {
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        "record_id": str(record.pk),
        "record_full_name": record.full_name,
        "member_display_name": user.get_display_name(),
        "member_email": user.email,
        "frontend_url": f"{frontend_base_url_for(organization)}/org/{organization.slug}/admin/members?tab=register",
    }
    transaction.on_commit(
        lambda: notify_org_staff(
            organization_id=organization.id,
            notification_type=NotificationType.STAFF_MEMBER_RECORD_AUTO_LINKED,
            context=context,
            sender=MembershipRecord,
        )
    )
