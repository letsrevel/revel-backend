"""Linking account-less register records to accounts (#1319).

A record without a user grants nothing. Linking sets ``MembershipRecord.user`` and then makes the access
row (``OrganizationMember``) agree; the register mirror in :mod:`events.service.member_register_service`
runs from the access row's ``post_save`` signal, so this module never imports it.

Two entry points reach :func:`link_record`: a staff-sent invitation link (:func:`claim_invite`, any account)
and the verified-email match (:func:`auto_link_records_for_user`).
"""

import enum
import hashlib
import secrets
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from events.models import MembershipRecord, MembershipRecordLinkMethod, Organization, OrganizationMember
from events.service import blacklist_service
from events.service.user_note_transfer import transfer_record_note
from events.suppression import is_membership_granted_suppressed, suppress_membership_granted
from events.utils import format_organization_datetime
from events.utils.frontend_urls import frontend_base_url_for
from notifications.enums import NotificationType
from notifications.membership_context_schemas import (
    MemberRecordLinkedContext,
    MembershipContext,
    StaffMemberRecordAutoLinkedContext,
)
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
    context: MembershipContext = {
        "organization_id": str(member.organization_id),
        "organization_name": organization.name,
        "role": "member",
        "action": "granted",
        "frontend_url": f"{frontend_base_url_for(organization)}/org/{organization.slug}",
    }
    notification_requested.send(
        sender=OrganizationMember,
        user=member.user,
        notification_type=NotificationType.MEMBERSHIP_GRANTED,
        context=context,
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
        if not is_membership_granted_suppressed():
            transaction.on_commit(lambda: notify_membership_granted(member))
        return member
    if member.status == Status.AWAITING_PAYMENT:
        # Held until the first payment: the claim keeps the linked record open and never touches the access row
        # (a save would make the mirror close the record). Activation reconciles tier and entry.
        return member
    # Legacy member without an open record (ACTIVE/PAUSED): keep tier and status, let the mirror reconcile.
    member.save(update_fields=["updated_at"])
    return member


def link_record(
    record: MembershipRecord,
    user: RevelUser,
    *,
    token: str | None = None,
    require_verified_email: bool = False,
    via: MembershipRecordLinkMethod = MembershipRecordLinkMethod.INVITE,
) -> LinkResult:
    """Link an account-less ``record`` to ``user``, applying the collision rules in order.

    Locks the claimant, then their access row, then the record (the mirror locks member -> record).
    When ``token`` is given it is re-checked against the locked record, so a link rotated or used
    between the caller's lookup and this lock reads as ALREADY_LINKED. With ``require_verified_email``
    (automatic links) the locked user must still be a non-guest holding a verified email equal (``iexact``) to
    the locked record's email, otherwise the call reads as ALREADY_LINKED too (nothing changes).
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

        # A revoked entry is dead for every link path (invite claims and automatic links alike).
        revoked = locked.ended_reason == MembershipRecord.EndedReason.REVOKED
        stale_token = token is not None and locked.invite_token != token
        # Same ``iexact`` the selection used, evaluated by Postgres (Python's upper() differs for some Unicode).
        email_mismatch = require_verified_email and (
            user.guest
            or not user.email_verified
            or not user.email
            or not MembershipRecord.objects.filter(pk=locked.pk, email__iexact=user.email).exists()
        )
        merged = locked.merged_into_id is not None  # merged after the caller's selection: it is not linkable
        if locked.user_id is not None or merged or revoked or stale_token or email_mismatch:
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
        locked.linked_via = via
        locked.linked_at = timezone.now()
        locked.save(
            update_fields=["user", "invite_token", "duplicate_claimant", "linked_via", "linked_at", "updated_at"]
        )
        transfer_record_note(locked, to_user_id=user.pk)
        if open_standard:
            # The verified-email auto-link tells the person with MEMBER_RECORD_LINKED, not MEMBERSHIP_GRANTED.
            auto = via == MembershipRecordLinkMethod.AUTO_EMAIL
            with suppress_membership_granted() if auto else nullcontext():
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
        .filter(
            organization_id=member.organization_id,
            kind=MembershipRecord.Kind.STANDARD,
            email__iexact=user.email,
            # A future-dated entry can't back access that starts now (same rule as NOT_STARTED on claims).
            valid_from__lte=timezone.now(),
        )
        .order_by("valid_from", "created_at")
        .first()
    )
    if record is None:
        return None
    record.user = user
    record.invite_token = None
    record.duplicate_claimant = None
    record.linked_via = MembershipRecord.LinkMethod.AUTO_EMAIL
    record.linked_at = timezone.now()
    record.save(update_fields=["user", "invite_token", "duplicate_claimant", "linked_via", "linked_at", "updated_at"])
    transfer_record_note(record, to_user_id=user.pk)
    logger.info("member_register_adopted_at_source", record_id=str(record.pk), user_id=str(user.pk))
    _notify_staff_of_auto_link(record, user)
    return record


# Minimum gap between two invitation emails to the same register entry.
MEMBER_REGISTER_INVITE_RESEND_COOLDOWN = timedelta(hours=1)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def invite_marker(token: str) -> str:
    """Non-secret identifier of one invitation generation (a truncated digest, so the token cannot be recovered)."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _dispatch_invite(record: MembershipRecord) -> None:
    from events.tasks.member_register import send_member_register_invite  # lazy: the task imports this module

    # The id travels through the broker, never the token: the task reads the current token itself. The
    # generation marker rides in a message header (the task signature stays one argument, so an old worker
    # during a rolling deploy still accepts the message) and the task skips when a later invitation superseded it.
    record_id, marker = str(record.pk), invite_marker(record.invite_token or "")
    transaction.on_commit(
        lambda: send_member_register_invite.apply_async(args=(record_id,), headers={"invite_marker": marker})
    )


def invite_blocked_reason(record: MembershipRecord) -> str | None:
    """Why ``record`` cannot be invited (translated), or None when it can. Reads fields only, no queries."""
    if record.user_id is not None:
        return str(_("This register entry is already linked to an account."))
    if record.merged_into_id is not None:
        return str(_("A merged register entry cannot be invited."))
    if not record.email:
        return str(_("This register entry has no email address to invite."))
    if record.ended_reason == MembershipRecord.EndedReason.REVOKED:
        return str(_("A revoked register entry cannot be invited."))
    if record.kind != MembershipRecord.Kind.STANDARD:
        return str(_("Only standard register entries can be invited."))
    if record.valid_until is not None:
        return str(_("An ended register entry cannot be invited."))
    if record.valid_from > timezone.now():
        return str(_("This register entry starts in the future; invite the person once it has started."))
    return None


def _invite_cooldown_key(organization_id: UUID, email: str) -> str:
    """Cache key of the resend interval: one per organization and (case-insensitive) address."""
    return f"member_register_invite:{organization_id}:{email.strip().casefold()}"


def _claim_invite_slot(organization: Organization, email: str) -> bool:
    """Atomically start the resend interval for ``email``; False if one is already running.

    The value is the send time, so a refusal can say when the next invite is allowed.
    """
    return cache.add(
        _invite_cooldown_key(organization.pk, email),
        timezone.now().timestamp(),
        int(MEMBER_REGISTER_INVITE_RESEND_COOLDOWN.total_seconds()),
    )


def _release_invite_slot(organization: Organization, email: str) -> None:
    cache.delete(_invite_cooldown_key(organization.pk, email))


def send_invite(record: MembershipRecord) -> MembershipRecord:
    """Send (or resend) the claim link for one record. A resend invalidates the previous link.

    At most one invitation per organization and address per ``MEMBER_REGISTER_INVITE_RESEND_COOLDOWN``,
    whichever record it goes through, so editing the address back and forth cannot be used to resend.
    """
    if reason := invite_blocked_reason(record):
        raise HttpError(400, reason)
    organization = record.organization
    if not _claim_invite_slot(organization, record.email):
        sent_at = cache.get(_invite_cooldown_key(organization.pk, record.email))
        next_at = (
            datetime.fromtimestamp(sent_at, tz=UTC) if sent_at else timezone.now()
        ) + MEMBER_REGISTER_INVITE_RESEND_COOLDOWN
        raise HttpError(
            400,
            str(_("An invitation was sent recently. You can resend it after {time}.")).format(
                time=format_organization_datetime(next_at, organization)
            ),
        )
    try:
        record.invite_token = _new_token()
        record.invited_at = timezone.now()
        record.save(update_fields=["invite_token", "invited_at", "updated_at"])
    except Exception:
        _release_invite_slot(organization, record.email)
        raise
    _dispatch_invite(record)
    return record


def send_bulk_invites(organization: Organization) -> int:
    """Invite every open STANDARD, unlinked, never-invited record that has an email. Returns the count.

    Closed records and already-invited ones are skipped; a resend is per record. An address that was
    invited within the resend interval (or appears on an earlier record of the same batch) is skipped too.
    """
    now = timezone.now()
    with transaction.atomic():
        candidates = list(
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
        records: list[MembershipRecord] = []
        try:
            for record in candidates:
                if _claim_invite_slot(organization, record.email):
                    records.append(record)
                    record.invite_token = _new_token()
                    record.invited_at = now
                    record.updated_at = now
            MembershipRecord.objects.bulk_update(records, ["invite_token", "invited_at", "updated_at"])
        except Exception:
            # Nothing committed: free the addresses so a retry is not locked out for the cooldown.
            for record in records:
                _release_invite_slot(organization, record.email)
            raise
        for record in records:
            _dispatch_invite(record)
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

    Never for guests or unverified emails. Staff are told about each successful link; the person gets one
    MEMBER_RECORD_LINKED notification per organization where a link leaves them an ACTIVE membership
    (links that touch only closed or TEMPORARY entries send nothing). MEMBERSHIP_GRANTED is not sent on this path.
    """
    user = RevelUser.objects.filter(pk=user_id).first()
    if user is None or user.guest or not user.email_verified or not user.email:
        return []
    records = list(
        MembershipRecord.objects.unlinked()
        .select_related("organization")
        .filter(email__iexact=user.email, valid_from__lte=timezone.now())  # never before an entry starts
        .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
        .order_by("valid_from", "created_at")
    )
    results: list[LinkResult] = []
    linked_organizations: dict[UUID, Organization] = {}
    for record in records:
        result = link_record(record, user, require_verified_email=True, via=MembershipRecord.LinkMethod.AUTO_EMAIL)
        results.append(result)
        logger.info("member_register_auto_link", record_id=str(record.pk), user_id=str(user.pk), outcome=result.outcome)
        if result.outcome == LinkOutcome.LINKED:
            _notify_staff_of_auto_link(result.record, user)
            if result.member is not None and result.member.status == Status.ACTIVE:
                linked_organizations.setdefault(result.record.organization_id, result.record.organization)
    for organization in linked_organizations.values():
        _notify_person_of_auto_link(organization, user)
    return results


def _notify_person_of_auto_link(organization: Organization, user: RevelUser) -> None:
    """Tell ``user`` after commit that a register entry of ``organization`` was linked to their account."""
    context: MemberRecordLinkedContext = {
        "organization_id": str(organization.id),
        "organization_name": organization.name,
        # ponytail: no self-service "leave" route exists yet; the organization page has the contact button.
        "frontend_url": f"{frontend_base_url_for(organization)}/org/{organization.slug}",
    }

    def notify_member_record_linked() -> None:
        notification_requested.send(
            sender=MembershipRecord,
            user=user,
            notification_type=NotificationType.MEMBER_RECORD_LINKED,
            context=context,
        )

    # Robust: a failed send must not stop the notifications for the other organizations.
    transaction.on_commit(notify_member_record_linked, robust=True)


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
