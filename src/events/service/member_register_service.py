"""Membership register service (#1318).

The register (``MembershipRecord``) is a legal log of membership periods. Access rows
(``OrganizationMember``) are mirrored into it from a single ``post_save`` receiver via
:func:`mirror_member`; callers describe *why* a change happens with :func:`record_source`.
"""

import contextlib
import contextvars
import typing as t
from datetime import datetime
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipRecord, MembershipTier, OrganizationMember
from events.models.membership_record import MembershipRecordQuerySet
from questionnaires.models import QuestionnaireSubmission

# ``None`` means no ``record_source`` block is active (records then default to STAFF, or inherit on re-open).
_source_var: contextvars.ContextVar[MembershipRecord.Source | None] = contextvars.ContextVar(
    "member_register_source", default=None
)
_submission_var: contextvars.ContextVar[QuestionnaireSubmission | None] = contextvars.ContextVar(
    "member_register_submission", default=None
)
_FULL_NAME_MAX = 255
_suppress_var: contextvars.ContextVar[bool] = contextvars.ContextVar("member_register_suppress", default=False)


@contextlib.contextmanager
def record_source(
    source: MembershipRecord.Source, *, questionnaire_submission: QuestionnaireSubmission | None = None
) -> t.Iterator[None]:
    """Declare the source (and acceptance evidence) for records opened inside the block."""
    source_token = _source_var.set(source)
    submission_token = _submission_var.set(questionnaire_submission)
    try:
        yield
    finally:
        _submission_var.reset(submission_token)
        _source_var.reset(source_token)


@contextlib.contextmanager
def suppress_mirror() -> t.Iterator[None]:
    """Skip mirroring access-row saves into the register inside the block."""
    token = _suppress_var.set(True)
    try:
        yield
    finally:
        _suppress_var.reset(token)


def _open_standard_qs(organization_id: UUID, user_id: UUID) -> MembershipRecordQuerySet:
    return MembershipRecord.objects.open().filter(
        organization_id=organization_id, user_id=user_id, kind=MembershipRecord.Kind.STANDARD
    )


def _snapshot_full_name(user: RevelUser) -> str:
    """Legal name first, truncated to the column width (first + last name can exceed 255)."""
    return (user.get_full_name() or user.get_display_name())[:_FULL_NAME_MAX]


def open_standard_record(member: OrganizationMember) -> MembershipRecord:
    """Return the open STANDARD record for the member, creating it if there is none."""
    with transaction.atomic():
        existing = _open_standard_qs(member.organization_id, member.user_id).select_for_update().first()
        if existing is not None:
            return existing

        user = member.user
        submission = _submission_var.get()
        accepted_at = None
        questionnaire_name = ""
        if submission is not None:
            accepted_at = submission.submitted_at or submission.created_at
            questionnaire_name = submission.questionnaire.name
        return MembershipRecord.objects.create(
            organization_id=member.organization_id,
            user=user,
            full_name=_snapshot_full_name(user),
            email=user.email,
            kind=MembershipRecord.Kind.STANDARD,
            tier_id=member.tier_id,
            tier_name=member.tier.name if member.tier else "",
            valid_from=timezone.now(),
            source=_source_var.get() or MembershipRecord.Source.STAFF,
            questionnaire_submission=submission,
            accepted_at=accepted_at,
            questionnaire_name=questionnaire_name,
        )


def close_standard_record(
    organization_id: UUID, user_id: UUID, *, reason: MembershipRecord.EndedReason, now: datetime | None = None
) -> MembershipRecord | None:
    """Close the open STANDARD record, if any. Returns it, or None when nothing was open."""
    with transaction.atomic():
        record = _open_standard_qs(organization_id, user_id).select_for_update().first()
        if record is None:
            return None
        # Never end before the start (clock skew / frozen clocks would violate the range constraint).
        record.valid_until = max(now or timezone.now(), record.valid_from)
        record.ended_reason = reason
        record.save(update_fields=["valid_until", "ended_reason", "updated_at"])
        return record


def _reopen_from(record: MembershipRecord, *, tier: MembershipTier | None, now: datetime) -> MembershipRecord:
    """Open the successor of a just-closed ``record`` at ``tier``, carrying its basis and evidence.

    A tier change does not change *why* the person is a member, so source, acceptance evidence and
    subscription are copied from ``record`` (``created_by`` is not: the successor was not made by that
    actor). When a ``record_source`` block is explicitly active, source and evidence come from it instead.
    """
    explicit_source = _source_var.get()
    source = record.source
    submission_id = record.questionnaire_submission_id
    accepted_at = record.accepted_at
    questionnaire_name = record.questionnaire_name
    submission = _submission_var.get()
    if explicit_source is not None:
        source = explicit_source
        submission_id = submission.pk if submission is not None else None
        accepted_at = (submission.submitted_at or submission.created_at) if submission is not None else None
        questionnaire_name = submission.questionnaire.name if submission is not None else ""
    return MembershipRecord.objects.create(
        organization_id=record.organization_id,
        user_id=record.user_id,
        full_name=record.full_name,
        email=record.email,
        kind=MembershipRecord.Kind.STANDARD,
        tier=tier,
        tier_name=tier.name if tier else "",
        valid_from=now,
        source=source,
        subscription_id=record.subscription_id,
        questionnaire_submission_id=submission_id,
        accepted_at=accepted_at,
        questionnaire_name=questionnaire_name,
    )


def mirror_member(member: OrganizationMember) -> None:
    """Reflect an access-row save into the register.

    ACTIVE/PAUSED rows keep exactly one open record (re-opened on tier change); CANCELLED/BANNED
    rows close it. The open record decides whether the tier changed.
    """
    if _suppress_var.get():
        return
    Status = OrganizationMember.MembershipStatus
    # The member row is locked and re-read before deciding: in autocommit contexts (Celery) the caller's
    # save has already committed, so concurrent saves of one member serialize here on the row lock and each
    # decides from the current DB state, not its possibly stale instance. The (org, user) partial unique
    # index still backstops the open record.
    with transaction.atomic():
        locked = (
            OrganizationMember.objects.select_for_update(of=("self",))
            .select_related("user", "tier")
            .filter(pk=member.pk)
            .first()
        )
        if locked is None:
            return
        open_record = _open_standard_qs(locked.organization_id, locked.user_id).first()
        if locked.status in (Status.ACTIVE, Status.PAUSED):
            if open_record is not None and open_record.tier_id != locked.tier_id:
                now = max(timezone.now(), open_record.valid_from)
                closed = close_standard_record(
                    locked.organization_id,
                    locked.user_id,
                    reason=MembershipRecord.EndedReason.TIER_CHANGED,
                    now=now,
                )
                if closed is not None:
                    _reopen_from(closed, tier=locked.tier, now=now)
                else:
                    open_standard_record(locked)
            elif open_record is None:
                open_standard_record(locked)
        elif open_record is not None:
            reason = (
                MembershipRecord.EndedReason.CANCELLED
                if locked.status == Status.CANCELLED
                else MembershipRecord.EndedReason.EXPELLED
            )
            close_standard_record(locked.organization_id, locked.user_id, reason=reason)


def reopen_for_tier_deletion(tier: MembershipTier) -> int:
    """Close-and-reopen every open STANDARD record at ``tier`` without a tier.

    Deleting a tier SET_NULLs ``OrganizationMember.tier`` with a bulk UPDATE (no signals), so
    the mirror never sees it. Call inside the same ``transaction.atomic()`` as ``tier.delete()``.

    Args:
        tier: The tier about to be deleted.

    Returns:
        The number of records re-opened.
    """
    count = 0
    with transaction.atomic():
        records = list(
            MembershipRecord.objects.open().filter(tier=tier, kind=MembershipRecord.Kind.STANDARD).select_for_update()
        )
        now = timezone.now()
        for record in records:
            end = max(now, record.valid_from)
            record.valid_until = end
            record.ended_reason = MembershipRecord.EndedReason.TIER_CHANGED
            record.save(update_fields=["valid_until", "ended_reason", "updated_at"])
            _reopen_from(record, tier=None, now=end)
            count += 1
    return count
