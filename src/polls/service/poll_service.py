"""Poll lifecycle service.

Function-based per the project's hybrid service-layer convention: these are
stateless single-purpose operations (create / open / close / reopen / update /
delete). All mutations that race with voting take a ``SELECT FOR UPDATE`` lock
on the poll row inside a ``transaction.atomic()`` block.
"""

import typing as t
from uuid import UUID

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from events.models.event import Event
from events.models.organization import MembershipTier, Organization
from polls.exceptions import (
    PollAnonymityImmutableError,
    PollLifecycleError,
    PollNotEligibleError,
    PollNotOpenError,
    PollValidationError,
    PollVoteAlreadyCastError,
    PollVoteChangesNotAllowedError,
)
from polls.models import Poll
from polls.schema import PollCreateSchema, PollReopenSchema, PollUpdateSchema, PollVoteSchema
from polls.utils import format_validation_error
from questionnaires.models import Questionnaire, QuestionnaireSubmission
from questionnaires.schema import QuestionnaireCreateSchema
from questionnaires.service import QuestionnaireService


def _translate_model_validation_error(exc: DjangoValidationError) -> t.NoReturn:
    """Convert a model-side ``DjangoValidationError`` into ``PollValidationError``.

    Our poll-specific exceptions inherit from :class:`DjangoValidationError`,
    so callers MUST filter their own subclasses out before invoking this
    helper — otherwise an immutability/lifecycle error would be silently
    flattened into a 422 validation response.
    """
    raise PollValidationError(format_validation_error(exc)) from exc


_POLL_ONLY_FIELDS: frozenset[str] = frozenset(
    {
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


def _resolve_event_or_raise(
    *,
    organization: Organization,
    event_id: UUID | None,
) -> Event | None:
    """Validate that ``event_id`` belongs to ``organization`` or raise.

    Mirrors :func:`_resolve_membership_tiers_or_raise` for event FK assignments:
    silently dropping (or attaching) a cross-org event would let a caller with
    ``manage_polls`` on org A move a poll to an event owned by org B.

    Args:
        organization: Organization the event must belong to.
        event_id: Optional event UUID. ``None`` short-circuits to ``None``.

    Returns:
        The resolved ``Event`` row, or ``None`` when ``event_id`` is ``None``.

    Raises:
        PollValidationError: when ``event_id`` does not resolve to an event
            owned by ``organization``.
    """
    if event_id is None:
        return None
    event = Event.objects.filter(pk=event_id, organization=organization).first()
    if event is None:
        raise PollValidationError(
            f"Unknown event {event_id} or it does not belong to this organization.",
        )
    return event


def _resolve_membership_tiers_or_raise(
    *,
    organization: Organization,
    tier_ids: t.Sequence[UUID],
) -> list[MembershipTier]:
    """Resolve ``tier_ids`` against ``organization`` or raise.

    Filters ``MembershipTier`` by organization + id-in and verifies every
    requested ID actually resolved. Catches both unknown IDs and cross-org
    leakage that would otherwise be silently dropped by ``.set(...)``.

    Args:
        organization: Organization the tiers must belong to.
        tier_ids: Tier IDs supplied by the caller. Duplicates are tolerated.

    Returns:
        The resolved ``MembershipTier`` rows.

    Raises:
        PollValidationError: when any requested ID does not resolve to a tier
            owned by ``organization``.
    """
    unique_ids = set(tier_ids)
    if not unique_ids:
        return []
    tiers = list(MembershipTier.objects.filter(organization=organization, id__in=unique_ids))
    if len(tiers) != len(unique_ids):
        resolved_ids = {tier.id for tier in tiers}
        missing = sorted(str(tid) for tid in unique_ids - resolved_ids)
        raise PollValidationError(
            f"Unknown or cross-organization membership_tier ids: {', '.join(missing)}",
        )
    return tiers


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
def create_poll(organization: Organization, payload: PollCreateSchema) -> Poll:
    """Create a Poll and its underlying Questionnaire.

    The Questionnaire is forced to ``evaluation_mode=MANUAL`` regardless of
    payload values — polls never invoke the evaluator pipeline.

    Args:
        organization: The organization the poll belongs to (taken from the
            request URL path; the controller already verified the caller has
            ``manage_polls`` on it).
        payload: Validated create payload (poll + questionnaire fields).

    Returns:
        The freshly created ``Poll`` in ``DRAFT`` status.

    Raises:
        PollValidationError: when ``vote_membership_tier_ids`` or
            ``result_membership_tier_ids`` reference unknown or
            cross-organization tier rows.
    """
    event = _resolve_event_or_raise(organization=organization, event_id=payload.event_id)

    questionnaire_payload = _build_questionnaire_schema(payload)
    questionnaire = QuestionnaireService.create_questionnaire(questionnaire_payload)

    # ``_build_questionnaire_schema`` already forces ``evaluation_mode=MANUAL``,
    # but re-assert it here as a defence-in-depth measure: if a future change
    # to QuestionnaireCreateSchema's defaults leaks through, polls must still
    # never trigger the evaluator pipeline.
    if questionnaire.evaluation_mode != Questionnaire.QuestionnaireEvaluationMode.MANUAL:
        questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.MANUAL
        questionnaire.save(update_fields=["evaluation_mode", "updated_at"])

    try:
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
    except PollAnonymityImmutableError:
        # Subclass of DjangoValidationError; preserve specific handler dispatch.
        raise
    except DjangoValidationError as exc:
        _translate_model_validation_error(exc)

    if payload.vote_membership_tier_ids:
        poll.vote_membership_tiers.set(
            _resolve_membership_tiers_or_raise(
                organization=organization,
                tier_ids=payload.vote_membership_tier_ids,
            )
        )
    if payload.result_membership_tier_ids:
        poll.result_membership_tiers.set(
            _resolve_membership_tiers_or_raise(
                organization=organization,
                tier_ids=payload.result_membership_tier_ids,
            )
        )
    return poll


def update_poll(poll: Poll, payload: PollUpdateSchema) -> Poll:
    """Apply a partial update under ``SELECT FOR UPDATE`` on the Poll row.

    Anonymity flags are not on ``PollUpdateSchema`` and the model itself
    raises ``PollAnonymityImmutableError`` if mutated post-create.

    ``name`` and ``description`` apply to the wrapped ``Questionnaire``
    (not the ``Poll``). They are applied within the same transaction as the
    poll-field updates so the two writes commit atomically.

    Args:
        poll: The poll to update (instance used only for its primary key).
        payload: PATCH schema with ``exclude_unset`` semantics — only the
            fields actually sent by the client are touched.

    Returns:
        The locked, updated ``Poll`` instance.

    Raises:
        PollValidationError: when ``vote_membership_tier_ids`` or
            ``result_membership_tier_ids`` reference unknown or
            cross-organization tier rows.
        django.core.exceptions.ValidationError: when the resulting row
            violates a model-level ``CheckConstraint`` (e.g., PRIVATE
            visibility with ``event_id=None``).
    """
    with transaction.atomic():
        locked = Poll.objects.select_for_update().get(pk=poll.pk)
        update_data = payload.model_dump(exclude_unset=True)
        tier_ids_vote = update_data.pop("vote_membership_tier_ids", None)
        tier_ids_result = update_data.pop("result_membership_tier_ids", None)
        # Pull questionnaire-owned fields out before applying poll-owned ones.
        questionnaire_updates = {key: update_data.pop(key) for key in ("name", "description") if key in update_data}

        # ``event_id`` needs cross-org validation BEFORE the setattr loop so a
        # caller can't move a poll to an event owned by a different org.
        if "event_id" in update_data:
            _resolve_event_or_raise(
                organization=locked.organization,
                event_id=update_data["event_id"],
            )

        _apply_poll_fields(locked, update_data)
        _apply_questionnaire_fields(locked, questionnaire_updates)
        _apply_tier_updates(locked, tier_ids_vote, tier_ids_result)
        return locked


def _apply_poll_fields(locked: Poll, update_data: dict[str, t.Any]) -> None:
    """Set + save poll-owned columns under the active SELECT FOR UPDATE."""
    if not update_data:
        return
    # ``event_id`` maps to the FK column directly; ``setattr`` on ``event_id``
    # is fine because Django exposes the FK attname.
    for field, value in update_data.items():
        setattr(locked, field, value)
    try:
        locked.save(update_fields=[*update_data.keys(), "updated_at"])
    except PollAnonymityImmutableError:
        raise
    except DjangoValidationError as exc:
        _translate_model_validation_error(exc)


def _apply_questionnaire_fields(locked: Poll, questionnaire_updates: dict[str, t.Any]) -> None:
    """Set + save wrapped-questionnaire columns under the active transaction."""
    if not questionnaire_updates:
        return
    questionnaire = locked.questionnaire
    for field, value in questionnaire_updates.items():
        setattr(questionnaire, field, value)
    try:
        questionnaire.save(update_fields=[*questionnaire_updates.keys(), "updated_at"])
    except DjangoValidationError as exc:
        _translate_model_validation_error(exc)


def _apply_tier_updates(
    locked: Poll,
    tier_ids_vote: t.Sequence[UUID] | None,
    tier_ids_result: t.Sequence[UUID] | None,
) -> None:
    """Apply explicit ``set(...)`` calls for membership-tier M2Ms when sent."""
    if tier_ids_vote is not None:
        locked.vote_membership_tiers.set(
            _resolve_membership_tiers_or_raise(
                organization=locked.organization,
                tier_ids=tier_ids_vote,
            )
        )
    if tier_ids_result is not None:
        locked.result_membership_tiers.set(
            _resolve_membership_tiers_or_raise(
                organization=locked.organization,
                tier_ids=tier_ids_result,
            )
        )


def open_poll(poll: Poll) -> Poll:
    """Move a ``DRAFT`` poll to ``OPEN``.

    Use :func:`reopen_poll` to revive a ``CLOSED`` poll.

    Args:
        poll: The poll to open (instance used only for its primary key).

    Returns:
        The locked, opened ``Poll`` instance.

    Raises:
        PollLifecycleError: when the poll is not in ``DRAFT`` status.
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
    """Move an ``OPEN`` poll to ``CLOSED``.

    Args:
        poll: The poll to close (instance used only for its primary key).

    Returns:
        The locked, closed ``Poll`` instance.

    Raises:
        PollLifecycleError: when the poll is not in ``OPEN`` status.
    """
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

    Args:
        poll: The poll to reopen (instance used only for its primary key).
        payload: Reopen schema with optional ``closes_at`` override or
            ``clear_closes_at`` flag.

    Returns:
        The locked, reopened ``Poll`` instance back in ``OPEN`` status.

    Raises:
        PollLifecycleError: when the poll is not ``CLOSED``, when the
            override ``closes_at`` is in the past, or when neither override
            is supplied and the existing ``closes_at`` is missing/past.
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

    Args:
        poll: The poll to delete. The function reads ``questionnaire_id``
            from the instance and deletes from the questionnaire side so the
            cascade can take care of the rest.

    Returns:
        None.
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

    Args:
        user: Authenticated user casting the vote.
        poll_id: Primary key of the target poll.
        payload: Multiple-choice / free-text / file-upload answers.

    Returns:
        The ``QuestionnaireSubmission`` (newly created or replaced).

    Raises:
        PollNotOpenError: when the poll is not ``OPEN``.
        PollNotEligibleError: when the user's audience doesn't include the
            poll's vote audience.
        PollVoteAlreadyCastError: when the user has already voted and
            ``allow_vote_changes`` is False.
        PollValidationError: when the payload references unknown or
            other-user file_upload ids.
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

    Args:
        user: Authenticated user withdrawing their vote.
        poll_id: Primary key of the target poll.

    Returns:
        None.

    Raises:
        PollNotOpenError: when the poll is not ``OPEN``.
        PollVoteChangesNotAllowedError: when the poll has
            ``allow_vote_changes=False``.
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
    :class:`PollValidationError` (mapped to 422 by the per-app exception
    handler) inside the outer ``transaction.atomic`` and roll back.
    """
    for mc in payload.mc_answers:
        _write_mc_answer(submission, mc)
    for ft in payload.free_text_answers:
        _write_free_text_answer(submission, ft)
    for fu in payload.file_upload_answers:
        _write_file_upload_answer(submission, fu)


def _write_mc_answer(submission: QuestionnaireSubmission, mc: t.Any) -> None:
    """Persist a single multiple-choice answer row (or raise on unknown ids)."""
    from questionnaires.models import MultipleChoiceAnswer, MultipleChoiceOption, MultipleChoiceQuestion

    try:
        mc_question = MultipleChoiceQuestion.objects.get(id=mc.question_id, questionnaire=submission.questionnaire)
    except MultipleChoiceQuestion.DoesNotExist as exc:
        raise PollValidationError(f"Unknown multiple-choice question id: {mc.question_id}") from exc
    for opt_id in mc.option_ids:
        try:
            option = MultipleChoiceOption.objects.get(id=opt_id, question=mc_question)
        except MultipleChoiceOption.DoesNotExist as exc:
            raise PollValidationError(f"Unknown multiple-choice option id: {opt_id}") from exc
        MultipleChoiceAnswer.objects.create(submission=submission, question=mc_question, option=option)


def _write_free_text_answer(submission: QuestionnaireSubmission, ft: t.Any) -> None:
    """Persist a single free-text answer row (or raise on unknown id)."""
    from questionnaires.models import FreeTextAnswer, FreeTextQuestion

    try:
        ft_question = FreeTextQuestion.objects.get(id=ft.question_id, questionnaire=submission.questionnaire)
    except FreeTextQuestion.DoesNotExist as exc:
        raise PollValidationError(f"Unknown free-text question id: {ft.question_id}") from exc
    FreeTextAnswer.objects.create(submission=submission, question=ft_question, answer=ft.answer)


def _write_file_upload_answer(submission: QuestionnaireSubmission, fu: t.Any) -> None:
    """Persist a single file-upload answer row + attach uploaded files."""
    from questionnaires.models import FileUploadAnswer, FileUploadQuestion, QuestionnaireFile

    try:
        fu_question = FileUploadQuestion.objects.get(id=fu.question_id, questionnaire=submission.questionnaire)
    except FileUploadQuestion.DoesNotExist as exc:
        raise PollValidationError(f"Unknown file-upload question id: {fu.question_id}") from exc
    ans = FileUploadAnswer.objects.create(submission=submission, question=fu_question)
    unique_file_ids = set(fu.file_ids)
    if not unique_file_ids:
        return
    files = list(QuestionnaireFile.objects.filter(uploader=submission.user, id__in=unique_file_ids))
    if len(files) != len(unique_file_ids):
        resolved = {f.id for f in files}
        missing = sorted(str(fid) for fid in unique_file_ids - resolved)
        raise PollValidationError(
            f"Unknown or other-user file_upload ids: {', '.join(missing)}",
        )
    ans.files.set(files)
