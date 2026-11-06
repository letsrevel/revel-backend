"""Membership register service (#1318).

The register (``MembershipRecord``) is a legal log of membership periods. Access rows
(``OrganizationMember``) are mirrored into it from a single ``post_save`` receiver via
:func:`mirror_member`; callers describe *why* a change happens with :func:`record_source`.
"""

import contextlib
import contextvars
import typing as t
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from events.models import MembershipRecord, OrganizationMember
from questionnaires.models import QuestionnaireSubmission

_source_var: contextvars.ContextVar[MembershipRecord.Source] = contextvars.ContextVar(
    "member_register_source", default=MembershipRecord.Source.STAFF
)
_submission_var: contextvars.ContextVar[QuestionnaireSubmission | None] = contextvars.ContextVar(
    "member_register_submission", default=None
)
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


def _open_standard_qs(organization_id: UUID, user_id: UUID) -> t.Any:
    return MembershipRecord.objects.open().filter(
        organization_id=organization_id, user_id=user_id, kind=MembershipRecord.Kind.STANDARD
    )


def open_standard_record(member: OrganizationMember) -> MembershipRecord:
    """Return the open STANDARD record for the member, creating it if there is none."""
    with transaction.atomic():
        existing = _open_standard_qs(member.organization_id, member.user_id).select_for_update().first()
        if existing is not None:
            return t.cast(MembershipRecord, existing)

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
            full_name=user.get_full_name() or user.get_display_name(),
            email=user.email,
            kind=MembershipRecord.Kind.STANDARD,
            tier_id=member.tier_id,
            tier_name=member.tier.name if member.tier else "",
            valid_from=timezone.now(),
            source=_source_var.get(),
            questionnaire_submission=submission,
            accepted_at=accepted_at,
            questionnaire_name=questionnaire_name,
        )


def close_standard_record(
    organization_id: UUID, user_id: UUID, *, reason: MembershipRecord.EndedReason
) -> MembershipRecord | None:
    """Close the open STANDARD record, if any. Returns it, or None when nothing was open."""
    with transaction.atomic():
        record = _open_standard_qs(organization_id, user_id).select_for_update().first()
        if record is None:
            return None
        record.valid_until = timezone.now()
        record.ended_reason = reason
        record.save(update_fields=["valid_until", "ended_reason", "updated_at"])
        return t.cast(MembershipRecord, record)


def mirror_member(member: OrganizationMember, *, old_tier_id: UUID | None) -> None:
    """Reflect an access-row save into the register.

    ACTIVE/PAUSED rows keep exactly one open record (re-opened on tier change); CANCELLED/BANNED
    rows close it. The open record, not ``old_tier_id``, decides whether the tier changed.
    """
    if _suppress_var.get():
        return
    Status = OrganizationMember.MembershipStatus
    with transaction.atomic():
        open_record = _open_standard_qs(member.organization_id, member.user_id).first()
        if member.status in (Status.ACTIVE, Status.PAUSED):
            if open_record is not None and open_record.tier_id != member.tier_id:
                close_standard_record(
                    member.organization_id, member.user_id, reason=MembershipRecord.EndedReason.TIER_CHANGED
                )
                open_standard_record(member)
            elif open_record is None:
                open_standard_record(member)
        elif open_record is not None:
            reason = (
                MembershipRecord.EndedReason.CANCELLED
                if member.status == Status.CANCELLED
                else MembershipRecord.EndedReason.EXPELLED
            )
            close_standard_record(member.organization_id, member.user_id, reason=reason)
