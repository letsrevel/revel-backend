"""Reconciling account-less register entries with accounts and linked entries (#1319).

Prevention at the source lives in :func:`events.service.member_register_linking.adopt_unlinked_record`.
This module covers what is left for staff: flagging possible matches for an application, listing possible
duplicates, merging an account-less entry into another entry, and unlinking (reversing a link or a merge).
It never imports :mod:`events.service.member_register_service`; that module may import this one's siblings.
"""

from dataclasses import dataclass, field
from uuid import UUID

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipRecord,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    RegisterMatchReason,
)
from events.utils.membership_access import visible_memberships
from events.utils.name_matching import normalize_name_for_matching

logger = structlog.get_logger(__name__)

Status = OrganizationMember.MembershipStatus


@dataclass
class RegisterMatch:
    """An unlinked register entry that may be the applicant, with why."""

    record: MembershipRecord
    reasons: list[RegisterMatchReason] = field(default_factory=list)


@dataclass
class PossibleDuplicate:
    """An unlinked entry that may be the same person as a linked open STANDARD entry."""

    unlinked: MembershipRecord
    linked: MembershipRecord
    reasons: list[RegisterMatchReason] = field(default_factory=list)


def _is_open_standard(record: MembershipRecord) -> bool:
    return record.valid_until is None and record.kind == MembershipRecord.Kind.STANDARD


def _user_full_name(user: RevelUser) -> str:
    return user.get_full_name() or user.get_display_name()


def register_matches_for_request(request: OrganizationMembershipRequest) -> list[RegisterMatch]:
    """Unlinked open STANDARD entries of the org that may be the applicant (email, name or collision claim)."""
    applicant = request.user
    email = applicant.email.casefold()
    name = normalize_name_for_matching(_user_full_name(applicant))
    # ponytail: name matching runs in Python over the org's unlinked open entries (a few thousand at most);
    # upgrade path is a normalized-name column.
    records = (
        MembershipRecord.objects.unlinked()
        .open()
        .select_related("tier")
        .filter(organization_id=request.organization_id, kind=MembershipRecord.Kind.STANDARD)
        .order_by("valid_from", "created_at")
    )
    matches: list[RegisterMatch] = []
    for record in records:
        reasons: list[RegisterMatchReason] = []
        if email and record.email.casefold() == email:
            reasons.append(RegisterMatchReason.EMAIL)
        if name and normalize_name_for_matching(record.full_name) == name:
            reasons.append(RegisterMatchReason.NAME)
        if record.duplicate_claimant_id == applicant.pk:
            reasons.append(RegisterMatchReason.CLAIM)
        if reasons:
            matches.append(RegisterMatch(record, reasons))
    return matches


def possible_duplicates(organization: Organization) -> list[PossibleDuplicate]:
    """Pair unlinked entries (open or closed, not merged) with linked open STANDARD entries, by reason."""
    # ponytail: in-memory join over two queries, fine for registers of a few thousand rows;
    # upgrade path is a SQL join on lower(email) / a normalized-name column.
    unlinked = list(
        MembershipRecord.objects.unlinked()
        .select_related("tier")
        .filter(organization=organization)
        .order_by("valid_from", "created_at")
    )
    linked = list(
        MembershipRecord.objects.open()
        .unmerged()
        .select_related("tier", "user")
        .filter(organization=organization, kind=MembershipRecord.Kind.STANDARD, user__isnull=False)
        .order_by("valid_from", "created_at")
    )
    by_email: dict[str, list[MembershipRecord]] = {}
    by_name: dict[str, list[MembershipRecord]] = {}
    by_user: dict[UUID, list[MembershipRecord]] = {}
    for rec in linked:
        user = rec.user
        assert user is not None and rec.user_id is not None  # filtered on user__isnull=False
        for email in {rec.email.casefold(), user.email.casefold()} - {""}:
            by_email.setdefault(email, []).append(rec)
        for name in {normalize_name_for_matching(rec.full_name), normalize_name_for_matching(_user_full_name(user))}:
            if name:
                by_name.setdefault(name, []).append(rec)
        by_user.setdefault(rec.user_id, []).append(rec)

    result: list[PossibleDuplicate] = []
    for rec in unlinked:
        hits: dict[UUID, PossibleDuplicate] = {}
        candidates = [
            (RegisterMatchReason.EMAIL, by_email.get(rec.email.casefold(), []) if rec.email else []),
            (RegisterMatchReason.NAME, by_name.get(normalize_name_for_matching(rec.full_name), [])),
            (
                RegisterMatchReason.CLAIM,
                by_user.get(rec.duplicate_claimant_id, []) if rec.duplicate_claimant_id else [],
            ),
        ]
        for reason, others in candidates:
            for other in others:
                pair = hits.setdefault(other.pk, PossibleDuplicate(rec, other))
                if reason not in pair.reasons:
                    pair.reasons.append(reason)
        result.extend(hits.values())
    return result


def _lock_members(organization_id: UUID, user_ids: list[UUID | None]) -> None:
    """Lock the access rows first (the mirror locks member -> record)."""
    list(
        OrganizationMember.objects.select_for_update(of=("self",))
        .filter(organization_id=organization_id, user_id__in=[u for u in user_ids if u is not None])
        .order_by("pk")
    )


def _lock_records(*pks: UUID | None) -> dict[UUID, MembershipRecord]:
    locked = (
        MembershipRecord.objects.select_for_update(of=("self",))
        .select_related("tier", "user")
        .filter(pk__in=[pk for pk in pks if pk is not None])
        .order_by("pk")
    )
    return {r.pk: r for r in locked}


def merge_records(survivor: MembershipRecord, absorbed: MembershipRecord) -> MembershipRecord:
    """Merge the account-less ``absorbed`` entry into ``survivor`` (same org, distinct, neither merged).

    Open STANDARD absorbed: the survivor (open STANDARD too) keeps its tier and snapshot and takes the earliest
    ``valid_from``; the absorbed entry is kept for audit (``merged_into``) and counts nowhere. Closed or TEMPORARY
    absorbed: it is just attached to the survivor's account. Anything else is a 400.
    """
    if survivor.pk == absorbed.pk:
        raise HttpError(400, str(_("A register entry cannot be merged into itself.")))
    if survivor.organization_id != absorbed.organization_id:
        raise HttpError(400, str(_("Register entries must belong to the same organization.")))
    with transaction.atomic():
        _lock_members(survivor.organization_id, [survivor.user_id])
        locked = _lock_records(survivor.pk, absorbed.pk)
        survivor, absorbed = locked[survivor.pk], locked[absorbed.pk]
        if survivor.merged_into_id is not None or absorbed.merged_into_id is not None:
            raise HttpError(400, str(_("A merged register entry cannot be merged again.")))
        if absorbed.absorbed_records.exists():
            raise HttpError(
                400, str(_("An entry that has absorbed other entries cannot be merged; unlink them first."))
            )
        if absorbed.user_id is not None:
            raise HttpError(400, str(_("Only an entry without an account can be merged into another entry.")))

        if _is_open_standard(absorbed):
            if not _is_open_standard(survivor):
                raise HttpError(400, str(_("An open entry can only be merged into another open standard entry.")))
            # Every entry absorbed by one survivor stores the survivor's *own* start (from before its first
            # merge), so any unlink order can recompute the start from what is still merged.
            own_start = (
                survivor.absorbed_records.exclude(merge_prior_valid_from=None)
                .values_list("merge_prior_valid_from", flat=True)
                .first()
            )
            absorbed.merge_prior_valid_from = own_start or survivor.valid_from
            absorbed.merged_into = survivor
            absorbed.invite_token = None
            absorbed.duplicate_claimant = None
            absorbed.save(
                update_fields=[
                    "merge_prior_valid_from",
                    "merged_into",
                    "invite_token",
                    "duplicate_claimant",
                    "updated_at",
                ]
            )
            survivor.valid_from = min(survivor.valid_from, absorbed.valid_from)
            # The survivor's own duplicate claim (if any) concerns another claimant: keep it for staff.
            survivor.save(update_fields=["valid_from", "updated_at"])
        else:
            if survivor.user_id is None:
                raise HttpError(400, str(_("A closed entry can only be attached to an entry linked to an account.")))
            absorbed.user = survivor.user
            absorbed.invite_token = None
            absorbed.duplicate_claimant = None
            absorbed.linked_via = MembershipRecord.LinkMethod.MERGE
            absorbed.linked_at = timezone.now()
            absorbed.save(
                update_fields=["user", "invite_token", "duplicate_claimant", "linked_via", "linked_at", "updated_at"]
            )
        logger.info("member_register_merged", survivor_id=str(survivor.pk), absorbed_id=str(absorbed.pk))
        return survivor


def unlink_record(record: MembershipRecord) -> MembershipRecord:
    """Reverse a merge or a link.

    Merged: the survivor's ``valid_from`` is restored and the entry is a plain unlinked entry again. Linked and
    open STANDARD: the entry loses its account and the account's ACTIVE/PAUSED access row is CANCELLED (a reversed
    link takes away the membership it granted). Linked and closed/TEMPORARY: only the account is removed.
    """
    with transaction.atomic():
        _lock_members(record.organization_id, [record.user_id])
        locked = _lock_records(record.pk, record.merged_into_id)
        current = locked[record.pk]
        if current.user_id != record.user_id:
            raise HttpError(409, str(_("This register entry was just linked to an account; reload it and try again.")))
        if current.merged_into_id is not None:
            survivor = (
                locked.get(current.merged_into_id) or _lock_records(current.merged_into_id)[current.merged_into_id]
            )
            others = list(
                MembershipRecord.objects.filter(merged_into=survivor)
                .exclude(pk=current.pk)
                .values_list("valid_from", flat=True)
            )
            # Recompute only while the survivor's start is still the merge-derived one (its own start or the
            # earliest absorbed start); a start staff moved since is theirs to keep.
            own_start = current.merge_prior_valid_from
            if own_start is not None and survivor.valid_from == min([own_start, current.valid_from, *others]):
                restored = min([own_start, *others])
                if survivor.valid_until is not None:
                    restored = min(restored, survivor.valid_until)
                survivor.valid_from = restored
                survivor.save(update_fields=["valid_from", "updated_at"])
            current.merged_into = None
            current.merge_prior_valid_from = None
            current.save(update_fields=["merged_into", "merge_prior_valid_from", "updated_at"])
            return current
        if current.user_id is None:
            raise HttpError(400, str(_("This register entry is not linked or merged.")))
        user_id = current.user_id
        was_open_standard = _is_open_standard(current)
        # The link goes first, so the mirror finds no open record for the account and leaves the register alone.
        current.user = None
        current.linked_via = None
        current.linked_at = None
        current.save(update_fields=["user", "linked_via", "linked_at", "updated_at"])
        if was_open_standard:
            member = (
                visible_memberships()
                .select_for_update(of=("self",))
                .filter(organization_id=current.organization_id, user_id=user_id)
                .first()
            )
            if member is not None:
                from events.service import organization_service  # lazy: avoid import cycle

                # Through the service so the member's subscriptions are cancelled too (otherwise billing continues).
                organization_service.update_member(member, status=Status.CANCELLED)
        logger.info("member_register_unlinked", record_id=str(current.pk))
        return current
