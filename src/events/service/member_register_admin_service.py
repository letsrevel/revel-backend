"""Staff-facing writes on the membership register (#1319).

The register is a legal log. Records linked to a live access row (``OrganizationMember``) are owned by
the mirror in :mod:`events.service.member_register_service`; these functions refuse edits that would
fight it and route the rest through the access row.
"""

import typing as t
from datetime import datetime
from uuid import UUID

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipRecord,
    MembershipRecordEndedReason,
    MembershipTier,
    Organization,
    OrganizationMember,
)

END_REASONS = (MembershipRecord.EndedReason.CANCELLED, MembershipRecord.EndedReason.REMOVED)


def _live_access_row(record: MembershipRecord) -> OrganizationMember | None:
    """The ACTIVE/PAUSED access row backing an open linked STANDARD record, if any.

    The member row is locked whatever its status, so a concurrent status change cannot slip in between
    this check and the caller's write. Callers lock it before the record: the mirror's member -> record order.
    """
    if record.user_id is None or record.kind != MembershipRecord.Kind.STANDARD or record.valid_until is not None:
        return None
    member = (
        OrganizationMember.objects.select_for_update(of=("self",))
        .select_related("user", "organization", "tier")
        .filter(organization_id=record.organization_id, user_id=record.user_id)
        .first()
    )
    Status = OrganizationMember.MembershipStatus
    if member is None or member.status not in (Status.ACTIVE, Status.PAUSED):
        return None
    return member


def _check_range(valid_from: datetime, valid_until: datetime | None) -> None:
    if valid_until is not None and valid_until < valid_from:
        raise HttpError(400, str(_("The end date cannot be before the start date.")))


def _resolve_tier(organization: Organization, tier_id: UUID) -> MembershipTier:
    # ponytail: MembershipTier has no ``kind`` yet; once it does, only STANDARD tiers may be chosen here.
    return get_object_or_404(MembershipTier, pk=tier_id, organization=organization)


def _lock(record: MembershipRecord) -> MembershipRecord:
    return MembershipRecord.objects.select_for_update(of=("self",)).select_related("tier").get(pk=record.pk)


@transaction.atomic
def create_manual_record(
    organization: Organization,
    *,
    full_name: str,
    email: str | None,
    tier_id: UUID,
    valid_from: datetime,
    valid_until: datetime | None,
    created_by: RevelUser,
) -> MembershipRecord:
    """Add a STANDARD, account-less register entry by hand."""
    _check_range(valid_from, valid_until)
    tier = _resolve_tier(organization, tier_id)
    return MembershipRecord.objects.create(
        organization=organization,
        user=None,
        full_name=full_name,
        email=email or "",
        kind=MembershipRecord.Kind.STANDARD,
        tier=tier,
        tier_name=tier.name,
        valid_from=valid_from,
        valid_until=valid_until,
        ended_reason=None,
        source=MembershipRecord.Source.MANUAL,
        created_by=created_by,
    )


def _apply_valid_until(record: MembershipRecord, new_until: datetime | None, *, linked_open: bool) -> list[str]:
    """Set ``valid_until`` on ``record`` (guarding linked records); returns the changed field names."""
    if record.user_id is not None and new_until is None and record.valid_until is not None:
        raise HttpError(409, str(_("A linked membership cannot be reopened from the register.")))
    if linked_open and new_until is not None:
        raise HttpError(409, str(_("End a linked membership with the end action instead.")))
    changed = ["valid_until"]
    if new_until is None and record.ended_reason is not None:
        record.ended_reason = None
        changed.append("ended_reason")
    record.valid_until = new_until
    return changed


@transaction.atomic
def update_record(record: MembershipRecord, payload_data: dict[str, t.Any]) -> MembershipRecord:
    """Apply a partial update (``model_dump(exclude_unset=True)`` of the update schema).

    Records without a user are fully editable (a hand-corrected legal log).
    """
    # ponytail: no overlap check against the person's other periods (e.g. a TIER_CHANGED successor);
    # staff corrections are trusted. Add one if overlapping periods start confusing "member on date X".
    record = _lock(record)
    linked_open = (
        record.user_id is not None and record.kind == MembershipRecord.Kind.STANDARD and record.valid_until is None
    )
    fields: list[str] = []

    if "tier_id" in payload_data and payload_data["tier_id"] != record.tier_id:
        if linked_open:
            raise HttpError(409, str(_("The tier of a linked membership is changed through member management.")))
        tier = _resolve_tier(record.organization, payload_data["tier_id"])
        record.tier, record.tier_name = tier, tier.name
        fields += ["tier", "tier_name"]
    if "full_name" in payload_data:
        record.full_name = payload_data["full_name"]
        fields.append("full_name")
    if "email" in payload_data:
        record.email = payload_data["email"] or ""
        fields.append("email")
    if "valid_from" in payload_data:
        if linked_open and payload_data["valid_from"] > timezone.now():
            raise HttpError(400, str(_("A linked membership cannot start in the future.")))
        record.valid_from = payload_data["valid_from"]
        fields.append("valid_from")
    if "valid_until" in payload_data:
        fields += _apply_valid_until(record, payload_data["valid_until"], linked_open=linked_open)

    _check_range(record.valid_from, record.valid_until)
    if fields:
        record.save(update_fields=[*dict.fromkeys(fields), "updated_at"])
    return record


@transaction.atomic
def end_record(
    record: MembershipRecord, *, reason: MembershipRecordEndedReason, valid_until: datetime | None
) -> MembershipRecord:
    """End a register entry; a linked live one is ended by cancelling the member."""
    if reason not in END_REASONS:
        raise HttpError(400, str(_("Reason must be 'cancelled' or 'removed'.")))
    member = _live_access_row(record)
    record = _lock(record)
    if record.valid_until is not None:
        raise HttpError(409, str(_("This membership has already ended.")))
    now = timezone.now()
    end = valid_until or now
    _check_range(record.valid_from, end)

    if member is not None:
        if end > now:
            raise HttpError(
                400,
                str(_("A linked membership cannot be ended in the future; end it now or change the member's status.")),
            )
        from events.service import organization_service  # lazy: avoid import cycle

        # The mirror compares state: ending only the record while the row stays ACTIVE/PAUSED would reopen it.
        organization_service.update_member(member, status=OrganizationMember.MembershipStatus.CANCELLED)
        record.refresh_from_db()
    record.valid_until = end
    record.ended_reason = reason
    record.save(update_fields=["valid_until", "ended_reason", "updated_at"])
    return record


@transaction.atomic
def delete_record(record: MembershipRecord) -> None:
    """Hard-delete a register entry, unless it is the open record of a live member."""
    if _live_access_row(record) is not None:
        raise HttpError(
            409, str(_("Remove the member from the organization before deleting their open register entry."))
        )
    _lock(record).delete()
