"""Event questionnaire submission service.

This module provides functions for submitting questionnaires through the events
endpoint with atomic tracking. It handles creating both the QuestionnaireSubmission
and EventQuestionnaireSubmission records in a single transaction, as well as
aggregate summary statistics for questionnaire results.
"""

from __future__ import annotations

import typing as t
from uuid import UUID

from django.db import transaction
from django.db.models import Avg, Count, Max, Min, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from events.models import Event, EventQuestionnaireSubmission, OrganizationQuestionnaire
from events.schema.questionnaire import (
    McOptionStatSchema,
    McQuestionStatSchema,
    QuestionnaireSummarySchema,
    ScoreStatsSchema,
    StatusBreakdownSchema,
)
from questionnaires.models import (
    MultipleChoiceOption,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
    SubmissionSourceEventMetadata,
)
from questionnaires.schema import QuestionnaireSubmissionSchema

if t.TYPE_CHECKING:
    from datetime import datetime

    from questionnaires.service.questionnaire_service import QuestionnaireService


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

        # Check can_retake_after cooldown
        if questionnaire.can_retake_after is None:
            raise HttpError(400, str(_("Retakes are not allowed for this questionnaire.")))

        # submitted_at is guaranteed to be set for READY submissions (see QuestionnaireSubmission.save())
        retry_on = t.cast("datetime", latest.submission.submitted_at) + questionnaire.can_retake_after
        if retry_on > timezone.now():
            raise HttpError(400, str(_("You can retry after %(retry_on)s.") % {"retry_on": retry_on}))

        # Cooldown elapsed and attempts remaining - allow submission


@transaction.atomic
def submit_event_questionnaire(
    *,
    user: RevelUser,
    event: Event,
    questionnaire_service: "QuestionnaireService",
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

    # Step 1: Build base queryset (scoped submission IDs)
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
        evt_ids = Event.objects.filter(event_series_id=event_series_id).values("id")
        sub_ids = EventQuestionnaireSubmission.objects.filter(
            questionnaire_id=questionnaire_id,
            event_id__in=evt_ids,
        ).values("submission_id")
        base_qs = base_qs.filter(id__in=sub_ids)

    # Step 2: Per-submission aggregation (single query, OneToOne JOIN)
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

    # Step 3: Per-user aggregation (latest submission per user via DISTINCT ON)
    latest_per_user_ids = base_qs.order_by("user_id", "-submitted_at").distinct("user_id").values("id")
    per_user_stats = QuestionnaireSubmission.objects.filter(
        id__in=latest_per_user_ids,
    ).aggregate(
        approved=Count("id", filter=Q(evaluation__status=EvalStatus.APPROVED)),
        rejected=Count("id", filter=Q(evaluation__status=EvalStatus.REJECTED)),
        pending_review=Count("id", filter=Q(evaluation__status=EvalStatus.PENDING_REVIEW)),
        not_evaluated=Count("id", filter=Q(evaluation__isnull=True)),
    )

    # Step 4: MC answer distributions
    # Start from all options for MC questions in this questionnaire, then LEFT JOIN answers
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

    # Group MC data by question
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
        mc_question_stats=list(questions_map.values()),
    )
