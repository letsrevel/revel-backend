"""Event questionnaire submission service.

This module provides functions for submitting questionnaires through the events
endpoint with atomic tracking. It handles creating both the QuestionnaireSubmission
and EventQuestionnaireSubmission records in a single transaction, as well as
aggregate summary statistics for questionnaire results.
"""

from __future__ import annotations

import typing as t
from uuid import UUID

from django.conf import settings
from django.db import models, transaction
from django.db.models import Avg, Count, Max, Min, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection, update_db_instance
from events.models import Event, EventQuestionnaireSubmission, EventSeries, Organization, OrganizationQuestionnaire
from events.schema import OrganizationQuestionnaireCreateSchema, OrganizationQuestionnaireUpdateSchema
from events.schema.pronouns import EventPronounDistributionSchema, PronounCountSchema
from events.schema.questionnaire import (
    McOptionStatSchema,
    McQuestionStatSchema,
    QuestionnaireSummarySchema,
    ScoreStatsSchema,
    StatusBreakdownSchema,
)
from questionnaires.models import (
    MultipleChoiceOption,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
    SubmissionSourceEventMetadata,
)
from questionnaires.schema import QuestionnaireSubmissionSchema
from questionnaires.service.questionnaire_service import QuestionnaireService

if t.TYPE_CHECKING:
    from datetime import datetime

    from common.models import FileExport


def _validate_admission_resubmission(
    *,
    user: RevelUser,
    event: Event,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Validate that user can submit an admission questionnaire.

    Mirrors the eligibility gate logic from QuestionnaireGate to ensure
    consistent validation at submission time.

    Note:
        This logic is duplicated from QuestionnaireGate in
        events/service/event_manager/gates.py. Any changes here should be
        reflected there and vice versa.

    Raises:
        HttpError: If user cannot submit due to pending/approved/rejected status.
    """
    questionnaire = org_questionnaire.questionnaire

    # Get all existing READY submissions for this user/event/questionnaire
    existing_submissions = list(
        EventQuestionnaireSubmission.objects.filter(
            user=user,
            event=event,
            questionnaire=questionnaire,
            submission__status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        .select_related("submission__evaluation")
        .order_by("-submission__submitted_at")
    )

    if not existing_submissions:
        return  # No previous submissions, allow

    # When evaluation is not required, only block if user already has a READY submission
    if not org_questionnaire.requires_evaluation:
        raise HttpError(400, str(_("You have already submitted this questionnaire.")))

    # Check the latest submission's evaluation status
    latest = existing_submissions[0]
    evaluation = getattr(latest.submission, "evaluation", None)

    # Case 1: No evaluation yet (pending async task) or PENDING_REVIEW
    if evaluation is None or evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW:
        raise HttpError(400, str(_("You have a submission pending evaluation.")))

    # Case 2: Already approved
    if evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED:
        raise HttpError(400, str(_("Your questionnaire has already been approved.")))

    # Case 3: Rejected - check retake eligibility
    if evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED:
        # Check max_attempts
        if 0 < questionnaire.max_attempts <= len(existing_submissions):
            raise HttpError(400, str(_("You have reached the maximum number of attempts.")))

        # Check can_retake_after cooldown (None or zero means immediate retake)
        if questionnaire.can_retake_after:
            # submitted_at is guaranteed to be set for READY submissions (see QuestionnaireSubmission.save())
            retry_on = t.cast("datetime", latest.submission.submitted_at) + questionnaire.can_retake_after
            if retry_on > timezone.now():
                raise HttpError(400, str(_("You can retry after %(retry_on)s.") % {"retry_on": retry_on}))

        # Cooldown elapsed (or no cooldown) and attempts remaining - allow submission


@transaction.atomic
def submit_event_questionnaire(
    *,
    user: RevelUser,
    event: Event,
    questionnaire_service: QuestionnaireService,
    org_questionnaire: OrganizationQuestionnaire,
    submission_schema: QuestionnaireSubmissionSchema,
) -> QuestionnaireSubmission:
    """Submit a questionnaire for an event with atomic tracking.

    Creates both the QuestionnaireSubmission and EventQuestionnaireSubmission
    tracking record in a single atomic transaction.

    Args:
        user: The user submitting the questionnaire.
        event: The event the questionnaire is being submitted for.
        questionnaire_service: The QuestionnaireService instance for the questionnaire.
        org_questionnaire: The OrganizationQuestionnaire wrapper.
        submission_schema: The submission data from the user.

    Returns:
        The created QuestionnaireSubmission.

    Raises:
        HttpError: If user cannot submit an admission questionnaire due to
            pending/approved/rejected status or retake restrictions.
    """
    # Validate admission questionnaire resubmission eligibility
    if (
        submission_schema.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
        and org_questionnaire.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.ADMISSION
    ):
        _validate_admission_resubmission(user=user, event=event, org_questionnaire=org_questionnaire)

    # Build source event metadata to store with the submission
    source_event: SubmissionSourceEventMetadata = {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start": event.start.isoformat() if event.start else "",
    }

    # Create the questionnaire submission (this is also atomic internally)
    db_submission = questionnaire_service.submit(user, submission_schema, source_event=source_event)

    # Create tracking record for ALL questionnaire types when status=ready
    if submission_schema.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY:
        if org_questionnaire.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.FEEDBACK:
            # For feedback: use race protection to enforce uniqueness (one submission per user/event)
            get_or_create_with_race_protection(
                EventQuestionnaireSubmission,
                Q(user=user, event=event, questionnaire=org_questionnaire.questionnaire),
                {
                    "user": user,
                    "event": event,
                    "questionnaire": org_questionnaire.questionnaire,
                    "submission": db_submission,
                    "questionnaire_type": org_questionnaire.questionnaire_type,
                },
            )
        else:
            # For other types (admission, membership, generic): always create
            # a new record since multiple submissions are allowed
            EventQuestionnaireSubmission.objects.create(
                user=user,
                event=event,
                questionnaire=org_questionnaire.questionnaire,
                submission=db_submission,
                questionnaire_type=org_questionnaire.questionnaire_type,
            )

    return db_submission


def _build_scoped_submission_qs(
    questionnaire_id: UUID,
    event_id: UUID | None,
    event_series_id: UUID | None,
) -> QuerySet[QuestionnaireSubmission]:
    """Build the base queryset of READY submissions, optionally scoped by event or series."""
    base_qs = QuestionnaireSubmission.objects.filter(
        questionnaire_id=questionnaire_id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    if event_id is not None:
        sub_ids = EventQuestionnaireSubmission.objects.filter(
            questionnaire_id=questionnaire_id,
            event_id=event_id,
        ).values("submission_id")
        base_qs = base_qs.filter(id__in=sub_ids)
    elif event_series_id is not None:
        evt_ids = Event.objects.exclude_templates().filter(event_series_id=event_series_id).values("id")
        sub_ids = EventQuestionnaireSubmission.objects.filter(
            questionnaire_id=questionnaire_id,
            event_id__in=evt_ids,
        ).values("submission_id")
        base_qs = base_qs.filter(id__in=sub_ids)

    return base_qs


def _aggregate_mc_distributions(
    questionnaire_id: UUID,
    base_qs: QuerySet[QuestionnaireSubmission],
) -> list[McQuestionStatSchema]:
    """Compute multiple-choice answer distributions for a questionnaire."""
    submission_ids = base_qs.values("id")
    mc_options = (
        MultipleChoiceOption.objects.filter(question__questionnaire_id=questionnaire_id)
        .values(
            "id",
            "option",
            "is_correct",
            "order",
            "question_id",
            "question__question",
            "question__order",
        )
        .annotate(
            count=Count(
                "answers",
                filter=Q(answers__submission_id__in=submission_ids),
            )
        )
        .order_by("question__order", "order")
    )

    questions_map: dict[UUID, McQuestionStatSchema] = {}
    for row in mc_options:
        qid = row["question_id"]
        if qid not in questions_map:
            questions_map[qid] = McQuestionStatSchema(
                question_id=qid,
                question_text=row["question__question"],
                options=[],
            )
        questions_map[qid].options.append(
            McOptionStatSchema(
                option_id=row["id"],
                option_text=row["option"],
                is_correct=row["is_correct"],
                count=row["count"],
            )
        )

    return list(questions_map.values())


def _aggregate_pronoun_distribution(
    base_qs: QuerySet[QuestionnaireSubmission],
) -> EventPronounDistributionSchema:
    """Compute pronoun distribution for users who submitted the questionnaire."""
    user_ids = base_qs.values_list("user_id", flat=True).distinct()
    pronoun_rows = (
        RevelUser.objects.filter(id__in=user_ids).values("pronouns").annotate(count=Count("id")).order_by("-count")
    )

    pronoun_dist: list[PronounCountSchema] = []
    total_with_pronouns = 0
    total_without_pronouns = 0
    for row in pronoun_rows:
        if row["pronouns"]:
            pronoun_dist.append(PronounCountSchema(pronouns=row["pronouns"], count=row["count"]))
            total_with_pronouns += row["count"]
        else:
            total_without_pronouns = row["count"]

    return EventPronounDistributionSchema(
        distribution=pronoun_dist,
        total_with_pronouns=total_with_pronouns,
        total_without_pronouns=total_without_pronouns,
        total_attendees=total_with_pronouns + total_without_pronouns,
    )


def get_questionnaire_summary(
    *,
    questionnaire_id: UUID,
    event_id: UUID | None = None,
    event_series_id: UUID | None = None,
) -> QuestionnaireSummarySchema:
    """Compute aggregate statistics for a questionnaire's submissions.

    Uses ID materialization to avoid cartesian products. Optionally filters
    submissions by event or event series.

    Args:
        questionnaire_id: The underlying Questionnaire ID.
        event_id: Optional event filter.
        event_series_id: Optional event series filter.

    Returns:
        A QuestionnaireSummarySchema with counts, score stats, and MC distributions.

    Raises:
        HttpError: If both event_id and event_series_id are provided.
    """
    if event_id is not None and event_series_id is not None:
        raise HttpError(400, str(_("Cannot filter by both event_id and event_series_id.")))

    base_qs = _build_scoped_submission_qs(questionnaire_id, event_id, event_series_id)

    # Per-submission aggregation (single query, OneToOne JOIN)
    EvalStatus = QuestionnaireEvaluation.QuestionnaireEvaluationStatus
    stats = base_qs.aggregate(
        total=Count("id"),
        unique_users=Count("user_id", distinct=True),
        approved=Count("id", filter=Q(evaluation__status=EvalStatus.APPROVED)),
        rejected=Count("id", filter=Q(evaluation__status=EvalStatus.REJECTED)),
        pending_review=Count("id", filter=Q(evaluation__status=EvalStatus.PENDING_REVIEW)),
        not_evaluated=Count("id", filter=Q(evaluation__isnull=True)),
        avg_score=Avg("evaluation__score", filter=Q(evaluation__score__isnull=False)),
        min_score=Min("evaluation__score", filter=Q(evaluation__score__isnull=False)),
        max_score=Max("evaluation__score", filter=Q(evaluation__score__isnull=False)),
    )

    # Per-user aggregation (latest submission per user via DISTINCT ON)
    latest_per_user_ids = base_qs.order_by("user_id", "-submitted_at", "-id").distinct("user_id").values("id")
    per_user_stats = QuestionnaireSubmission.objects.filter(
        id__in=latest_per_user_ids,
    ).aggregate(
        approved=Count("id", filter=Q(evaluation__status=EvalStatus.APPROVED)),
        rejected=Count("id", filter=Q(evaluation__status=EvalStatus.REJECTED)),
        pending_review=Count("id", filter=Q(evaluation__status=EvalStatus.PENDING_REVIEW)),
        not_evaluated=Count("id", filter=Q(evaluation__isnull=True)),
    )

    return QuestionnaireSummarySchema(
        total_submissions=stats["total"],
        unique_users=stats["unique_users"],
        by_status=StatusBreakdownSchema(
            approved=stats["approved"],
            rejected=stats["rejected"],
            pending_review=stats["pending_review"],
            not_evaluated=stats["not_evaluated"],
        ),
        by_status_per_user=StatusBreakdownSchema(
            approved=per_user_stats["approved"],
            rejected=per_user_stats["rejected"],
            pending_review=per_user_stats["pending_review"],
            not_evaluated=per_user_stats["not_evaluated"],
        ),
        score_stats=ScoreStatsSchema(
            avg=stats["avg_score"],
            min=stats["min_score"],
            max=stats["max_score"],
        ),
        mc_question_stats=_aggregate_mc_distributions(questionnaire_id, base_qs),
        pronoun_distribution=_aggregate_pronoun_distribution(base_qs),
    )


def validate_feedback_requires_evaluation(
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType,
    requires_evaluation: bool,
) -> None:
    """Raise 400 if a feedback questionnaire is configured to require evaluation."""
    if questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.FEEDBACK and requires_evaluation:
        raise HttpError(400, str(_("Feedback questionnaires cannot require evaluation.")))


@transaction.atomic
def update_organization_questionnaire(
    org_questionnaire: "models.Model",  # OrganizationQuestionnaire
    payload: OrganizationQuestionnaireUpdateSchema,
) -> "models.Model":
    """Update organization questionnaire and its underlying questionnaire.

    Handles updating both OrganizationQuestionnaire wrapper fields and the underlying
    Questionnaire fields, including necessary type conversions. Uses update_db_instance
    for transaction safety and row-level locking.

    Args:
        org_questionnaire: The OrganizationQuestionnaire instance to update
        payload: The update payload containing fields to modify

    Returns:
        The updated OrganizationQuestionnaire instance
    """
    # Validate feature flag: block LLM evaluation modes when the questionnaire has free-text questions
    if not settings.FEATURE_LLM_EVALUATION and payload.evaluation_mode in [
        Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
        Questionnaire.QuestionnaireEvaluationMode.HYBRID,
    ]:
        questionnaire = org_questionnaire.questionnaire  # type: ignore[attr-defined]
        has_free_text = questionnaire.freetextquestion_questions.exists()
        if not has_free_text:
            has_free_text = any(section.freetextquestion_questions.exists() for section in questionnaire.sections.all())
        if has_free_text:
            raise HttpError(400, "LLM evaluation is not available.")

    # Extract questionnaire-specific fields with type conversions
    questionnaire_kwargs = {}
    payload_dict = payload.model_dump(exclude_unset=True)

    # Map fields that belong to Questionnaire
    questionnaire_field_names = {
        "name",
        "min_score",
        "shuffle_questions",
        "shuffle_sections",
        "evaluation_mode",
        "llm_guidelines",
        "max_attempts",
        "can_retake_after",
    }
    for field_name in questionnaire_field_names:
        if field_name in payload_dict:
            questionnaire_kwargs[field_name] = payload_dict[field_name]

    # Update the underlying Questionnaire if there are changes
    if questionnaire_kwargs:
        update_db_instance(
            org_questionnaire.questionnaire,  # type: ignore[attr-defined]
            payload=None,
            exclude_unset=False,
            exclude_defaults=False,
            **questionnaire_kwargs,
        )

    # Extract OrganizationQuestionnaire-specific fields
    org_kwargs = {}
    org_field_names = {"max_submission_age", "questionnaire_type", "members_exempt", "per_event", "requires_evaluation"}
    for field_name in org_field_names:
        if field_name in payload_dict:
            org_kwargs[field_name] = payload_dict[field_name]

    # Validate: feedback questionnaires cannot require evaluation
    effective_type = org_kwargs.get("questionnaire_type", org_questionnaire.questionnaire_type)  # type: ignore[attr-defined]
    effective_requires_eval = org_kwargs.get("requires_evaluation", org_questionnaire.requires_evaluation)  # type: ignore[attr-defined]
    validate_feedback_requires_evaluation(
        t.cast(OrganizationQuestionnaire.QuestionnaireType, effective_type),
        t.cast(bool, effective_requires_eval),
    )

    # Update OrganizationQuestionnaire if there are changes
    if org_kwargs:
        org_questionnaire = update_db_instance(
            org_questionnaire, payload=None, exclude_unset=False, exclude_defaults=False, **org_kwargs
        )

    # Refresh to get updated related questionnaire
    org_questionnaire.refresh_from_db()

    return org_questionnaire


@transaction.atomic
def create_org_questionnaire(
    organization: Organization,
    payload: OrganizationQuestionnaireCreateSchema,
) -> OrganizationQuestionnaire:
    """Create a questionnaire and its OrganizationQuestionnaire wrapper atomically.

    Validates that feedback questionnaires cannot require evaluation, creates the underlying
    Questionnaire (with its sections/questions/options), then wraps it in an
    OrganizationQuestionnaire bound to the given organization.

    Args:
        organization: The organization that will own the questionnaire.
        payload: The create schema with both Questionnaire and OrganizationQuestionnaire fields.

    Returns:
        The created OrganizationQuestionnaire.

    Raises:
        HttpError: 400 if the configuration is invalid (e.g. feedback + requires_evaluation).
    """
    validate_feedback_requires_evaluation(payload.questionnaire_type, payload.requires_evaluation)
    questionnaire = QuestionnaireService.create_questionnaire(payload)
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        max_submission_age=payload.max_submission_age,
        questionnaire_type=payload.questionnaire_type,
        members_exempt=payload.members_exempt,
        per_event=payload.per_event,
        requires_evaluation=payload.requires_evaluation,
    )


def set_status(
    org_questionnaire: OrganizationQuestionnaire,
    status: Questionnaire.QuestionnaireStatus,
) -> OrganizationQuestionnaire:
    """Update the status of the underlying Questionnaire.

    Args:
        org_questionnaire: The OrganizationQuestionnaire wrapping the questionnaire to update.
        status: The target status (DRAFT, READY, PUBLISHED).

    Returns:
        The refreshed OrganizationQuestionnaire with updated nested questionnaire status.
    """
    org_questionnaire.questionnaire.status = status
    org_questionnaire.questionnaire.save(update_fields=["status"])
    org_questionnaire.refresh_from_db()
    return org_questionnaire


def replace_events(
    org_questionnaire: OrganizationQuestionnaire,
    event_ids: list[UUID],
) -> OrganizationQuestionnaire:
    """Replace the set of events assigned to this questionnaire.

    Validates that every supplied event id belongs to the same organization as the
    questionnaire before performing the batch assignment.

    Args:
        org_questionnaire: The OrganizationQuestionnaire to update.
        event_ids: The complete list of event ids that should be assigned.

    Returns:
        The OrganizationQuestionnaire with its events relationship replaced.

    Raises:
        HttpError: 400 if any id does not match an event in the questionnaire's organization.
    """
    events = Event.objects.filter(pk__in=event_ids, organization=org_questionnaire.organization)
    if events.count() != len(set(event_ids)):
        raise HttpError(400, str(_("One or more events do not exist or belong to this organization.")))

    org_questionnaire.events.set(events)
    return org_questionnaire


def replace_event_series(
    org_questionnaire: OrganizationQuestionnaire,
    event_series_ids: list[UUID],
) -> OrganizationQuestionnaire:
    """Replace the set of event series assigned to this questionnaire.

    Validates that every supplied event series id belongs to the same organization as the
    questionnaire before performing the batch assignment.

    Args:
        org_questionnaire: The OrganizationQuestionnaire to update.
        event_series_ids: The complete list of event series ids that should be assigned.

    Returns:
        The OrganizationQuestionnaire with its event_series relationship replaced.

    Raises:
        HttpError: 400 if any id does not match a series in the questionnaire's organization.
    """
    series = EventSeries.objects.filter(pk__in=event_series_ids, organization=org_questionnaire.organization)
    if series.count() != len(set(event_series_ids)):
        raise HttpError(400, str(_("One or more event series do not exist or belong to this organization.")))

    org_questionnaire.event_series.set(series)
    return org_questionnaire


def start_submissions_export(
    org_questionnaire: OrganizationQuestionnaire,
    requested_by: RevelUser,
    event_id: UUID | None = None,
    event_series_id: UUID | None = None,
) -> "FileExport":
    """Create a FileExport record for questionnaire submissions and dispatch the export task.

    Args:
        org_questionnaire: The questionnaire whose submissions should be exported.
        requested_by: The user requesting the export (recorded on the FileExport).
        event_id: Optional event id to scope the export to a single event.
        event_series_id: Optional event series id to scope the export to a series.

    Returns:
        The newly-created FileExport in PENDING state.

    Raises:
        HttpError: 400 if both event_id and event_series_id are supplied (mutually exclusive).
    """
    from common.models import FileExport
    from events.tasks import generate_questionnaire_export_task

    if event_id and event_series_id:
        raise HttpError(400, str(_("Cannot filter by both event_id and event_series_id.")))

    parameters: dict[str, str] = {
        "questionnaire_id": str(org_questionnaire.questionnaire_id),
    }
    if event_id:
        parameters["event_id"] = str(event_id)
    if event_series_id:
        parameters["event_series_id"] = str(event_series_id)

    export = FileExport.objects.create(
        requested_by=requested_by,
        export_type=FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS,
        parameters=parameters,
    )
    generate_questionnaire_export_task.delay(str(export.id))
    return export
