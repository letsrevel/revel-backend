"""Poll lifecycle service.

Function-based per the project's hybrid service-layer convention: these are
stateless single-purpose operations (create / open / close / reopen / update /
delete). All mutations that race with voting take a ``SELECT FOR UPDATE`` lock
on the poll row inside a ``transaction.atomic()`` block.
"""

import typing as t
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from events.models.event import Event
from events.models.organization import MembershipTier, Organization
from polls.exceptions import (
    PollLifecycleError,
    PollNotEligibleError,
    PollNotOpenError,
    PollVoteAlreadyCastError,
    PollVoteChangesNotAllowedError,
)
from polls.models import Poll
from polls.schema import PollCreateSchema, PollReopenSchema, PollUpdateSchema, PollVoteSchema
from questionnaires.models import Questionnaire, QuestionnaireSubmission
from questionnaires.schema import QuestionnaireCreateSchema
from questionnaires.service import QuestionnaireService

_POLL_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "organization_id",
        "event_id",
        "vote_visibility",
        "result_visibility",
        "result_timing",
        "vote_membership_tier_ids",
        "result_membership_tier_ids",
        "staff_anonymous",
        "public_anonymous",
        "allow_vote_changes",
        "closes_at",
    }
)


def _build_questionnaire_schema(payload: PollCreateSchema) -> QuestionnaireCreateSchema:
    """Build a clean ``QuestionnaireCreateSchema`` from a poll-create payload.

    Polls force ``evaluation_mode=MANUAL`` server-side; ``min_score`` is
    irrelevant for polls (kept at its schema default of zero). All poll-only
    fields are stripped here so the questionnaire service sees only what it
    expects.
    """
    data: dict[str, t.Any] = payload.model_dump(exclude=set(_POLL_ONLY_FIELDS))
    # The serialized form converts ``can_retake_after`` (timedelta) to int via
    # the field_serializer; pass the raw value through so the questionnaire
    # service stores a proper timedelta.
    data["can_retake_after"] = payload.can_retake_after
    # Force the silent defaults regardless of what the client supplied.
    data["evaluation_mode"] = Questionnaire.QuestionnaireEvaluationMode.MANUAL
    # Reuse the validated nested objects directly (model_dump turns them into
    # dicts, which QuestionnaireCreateSchema would re-validate happily, but
    # passing dicts is fine because Pydantic will coerce them).
    return QuestionnaireCreateSchema(**data)


@transaction.atomic
def create_poll(payload: PollCreateSchema) -> Poll:
    """Create a Poll and its underlying Questionnaire.

    The Questionnaire is forced to ``evaluation_mode=MANUAL`` regardless of
    payload values — polls never invoke the evaluator pipeline.
    """
    organization = Organization.objects.get(pk=payload.organization_id)
    event = Event.objects.get(pk=payload.event_id) if payload.event_id else None

    questionnaire_payload = _build_questionnaire_schema(payload)
    questionnaire = QuestionnaireService.create_questionnaire(questionnaire_payload)

    # ``_build_questionnaire_schema`` already forces ``evaluation_mode=MANUAL``,
    # but re-assert it here as a defence-in-depth measure: if a future change
    # to QuestionnaireCreateSchema's defaults leaks through, polls must still
    # never trigger the evaluator pipeline.
    if questionnaire.evaluation_mode != Questionnaire.QuestionnaireEvaluationMode.MANUAL:
        questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.MANUAL
        questionnaire.save(update_fields=["evaluation_mode", "updated_at"])

    poll = Poll.objects.create(
        organization=organization,
        event=event,
        questionnaire=questionnaire,
        vote_visibility=payload.vote_visibility,
        result_visibility=payload.result_visibility,
        result_timing=payload.result_timing,
        staff_anonymous=payload.staff_anonymous,
        public_anonymous=payload.public_anonymous,
        allow_vote_changes=payload.allow_vote_changes,
        closes_at=payload.closes_at,
    )

    if payload.vote_membership_tier_ids:
        poll.vote_membership_tiers.set(
            MembershipTier.objects.filter(organization=organization, id__in=payload.vote_membership_tier_ids)
        )
    if payload.result_membership_tier_ids:
        poll.result_membership_tiers.set(
            MembershipTier.objects.filter(organization=organization, id__in=payload.result_membership_tier_ids)
        )
    return poll


def update_poll(poll: Poll, payload: PollUpdateSchema) -> Poll:
    """Apply a partial update.

    Anonymity flags are not on ``PollUpdateSchema`` and the model itself
    raises ``PollAnonymityImmutableError`` if mutated post-create.
    """
    with transaction.atomic():
        locked = Poll.objects.select_for_update().get(pk=poll.pk)
        update_data = payload.model_dump(exclude_unset=True)
        tier_ids_vote = update_data.pop("vote_membership_tier_ids", None)
        tier_ids_result = update_data.pop("result_membership_tier_ids", None)

        # ``event_id`` maps to the FK column directly; ``setattr`` on
        # ``event_id`` is fine because Django exposes the FK attname.
        for field, value in update_data.items():
            setattr(locked, field, value)
        if update_data:
            locked.save(update_fields=[*update_data.keys(), "updated_at"])

        if tier_ids_vote is not None:
            locked.vote_membership_tiers.set(
                MembershipTier.objects.filter(organization=locked.organization, id__in=tier_ids_vote)
            )
        if tier_ids_result is not None:
            locked.result_membership_tiers.set(
                MembershipTier.objects.filter(organization=locked.organization, id__in=tier_ids_result)
            )
        return locked


def open_poll(poll: Poll) -> Poll:
    """Move a ``DRAFT`` poll to ``OPEN``.

    Use :func:`reopen_poll` to revive a ``CLOSED`` poll.
    """
    with transaction.atomic():
        locked = Poll.objects.select_for_update().get(pk=poll.pk)
        if locked.status != Poll.PollStatus.DRAFT:
            raise PollLifecycleError("Only DRAFT polls can be opened directly. Use reopen for CLOSED polls.")
        locked.status = Poll.PollStatus.OPEN
        locked.opened_at = timezone.now()
        locked.save(update_fields=["status", "opened_at", "updated_at"])
        return locked


def close_poll(poll: Poll) -> Poll:
    """Move an ``OPEN`` poll to ``CLOSED``."""
    with transaction.atomic():
        locked = Poll.objects.select_for_update().get(pk=poll.pk)
        if locked.status != Poll.PollStatus.OPEN:
            raise PollLifecycleError("Only OPEN polls can be closed.")
        locked.status = Poll.PollStatus.CLOSED
        locked.closed_at = timezone.now()
        locked.save(update_fields=["status", "closed_at", "updated_at"])
        return locked


def reopen_poll(poll: Poll, payload: PollReopenSchema) -> Poll:
    """Reopen a ``CLOSED`` poll.

    The caller must either provide a future ``closes_at`` or set
    ``clear_closes_at=True`` to drop the deadline. Otherwise the existing
    ``closes_at`` must still be in the future — reopening with a past
    deadline would close the poll immediately on the next auto-close pass.
    """
    with transaction.atomic():
        locked = Poll.objects.select_for_update().get(pk=poll.pk)
        if locked.status != Poll.PollStatus.CLOSED:
            raise PollLifecycleError("Only CLOSED polls can be reopened.")

        if payload.clear_closes_at:
            locked.closes_at = None
        elif payload.closes_at is not None:
            if payload.closes_at <= timezone.now():
                raise PollLifecycleError("closes_at must be in the future.")
            locked.closes_at = payload.closes_at
        else:
            # Neither override given: the existing closes_at must be a
            # meaningful future deadline.
            if locked.closes_at is None or locked.closes_at <= timezone.now():
                raise PollLifecycleError("Cannot reopen: provide a future closes_at or set clear_closes_at=True.")

        locked.status = Poll.PollStatus.OPEN
        locked.closed_at = None
        locked.save(update_fields=["status", "closes_at", "closed_at", "updated_at"])
        return locked


def delete_poll(poll: Poll) -> None:
    """Hard-delete the poll and its questionnaire.

    The ``Poll.questionnaire`` relation is ``OneToOneField(on_delete=CASCADE)``
    from poll to questionnaire — deleting the questionnaire cascades to the
    poll and to all ``QuestionnaireSubmission`` rows (votes).
    """
    with transaction.atomic():
        Questionnaire.objects.filter(pk=poll.questionnaire_id).delete()


# ----- vote / withdraw -----


def vote(
    *,
    user: t.Any,
    poll_id: UUID,
    payload: PollVoteSchema,
) -> QuestionnaireSubmission:
    """Cast or replace a vote.

    Acquires ``SELECT FOR UPDATE`` on the Poll row so that close races resolve
    deterministically. Single submission per (user, questionnaire); replaced
    in place when ``allow_vote_changes`` is True.
    """
    from polls.service import eligibility as _eligibility

    with transaction.atomic():
        poll = Poll.objects.select_for_update().get(pk=poll_id)

        if poll.status != Poll.PollStatus.OPEN:
            raise PollNotOpenError()
        if not _eligibility.can_vote(user, poll):
            raise PollNotEligibleError()

        existing = QuestionnaireSubmission.objects.filter(user=user, questionnaire=poll.questionnaire).first()
        if existing is not None and not poll.allow_vote_changes:
            raise PollVoteAlreadyCastError()

        if existing is None:
            submission = QuestionnaireSubmission.objects.create(
                user=user,
                questionnaire=poll.questionnaire,
                status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
                submitted_at=timezone.now(),
            )
        else:
            submission = existing
            submission.status = QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
            submission.submitted_at = timezone.now()
            submission.save(update_fields=["status", "submitted_at", "updated_at"])
            _clear_answers(submission)

        _write_answers(submission, payload)
        return submission


def withdraw_vote(*, user: t.Any, poll_id: UUID) -> None:
    """Delete the user's submission for the poll.

    Only valid while the poll is OPEN AND ``allow_vote_changes=True``.
    """
    with transaction.atomic():
        poll = Poll.objects.select_for_update().get(pk=poll_id)
        if poll.status != Poll.PollStatus.OPEN:
            raise PollNotOpenError()
        if not poll.allow_vote_changes:
            raise PollVoteChangesNotAllowedError()
        QuestionnaireSubmission.objects.filter(user=user, questionnaire=poll.questionnaire).delete()


def _clear_answers(submission: QuestionnaireSubmission) -> None:
    """Remove all answers tied to a submission prior to rewriting them."""
    from questionnaires.models import FileUploadAnswer, FreeTextAnswer, MultipleChoiceAnswer

    MultipleChoiceAnswer.objects.filter(submission=submission).delete()
    FreeTextAnswer.objects.filter(submission=submission).delete()
    FileUploadAnswer.objects.filter(submission=submission).delete()


def _write_answers(submission: QuestionnaireSubmission, payload: PollVoteSchema) -> None:
    """Materialise the vote payload as answer rows under ``submission``.

    Validates that referenced questions belong to the submission's questionnaire
    (and options to their question) via scoped lookups — bogus IDs raise
    ``DoesNotExist`` inside the outer ``transaction.atomic`` and roll back.
    """
    from questionnaires.models import (
        FileUploadAnswer,
        FileUploadQuestion,
        FreeTextAnswer,
        FreeTextQuestion,
        MultipleChoiceAnswer,
        MultipleChoiceOption,
        MultipleChoiceQuestion,
        QuestionnaireFile,
    )

    for mc in payload.mc_answers:
        mc_question = MultipleChoiceQuestion.objects.get(id=mc.question_id, questionnaire=submission.questionnaire)
        for opt_id in mc.option_ids:
            option = MultipleChoiceOption.objects.get(id=opt_id, question=mc_question)
            MultipleChoiceAnswer.objects.create(submission=submission, question=mc_question, option=option)

    for ft in payload.free_text_answers:
        ft_question = FreeTextQuestion.objects.get(id=ft.question_id, questionnaire=submission.questionnaire)
        FreeTextAnswer.objects.create(submission=submission, question=ft_question, answer=ft.answer)

    for fu in payload.file_upload_answers:
        fu_question = FileUploadQuestion.objects.get(id=fu.question_id, questionnaire=submission.questionnaire)
        ans = FileUploadAnswer.objects.create(submission=submission, question=fu_question)
        ans.files.set(QuestionnaireFile.objects.filter(uploader=submission.user, id__in=fu.file_ids))
