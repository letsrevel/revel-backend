"""Event questionnaire submission service.

This module provides functions for submitting questionnaires through the events
endpoint with atomic tracking. It handles creating both the QuestionnaireSubmission
and EventQuestionnaireSubmission records in a single transaction.
"""

from __future__ import annotations

import typing as t

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from events.models import Event, EventQuestionnaireSubmission, OrganizationQuestionnaire
from questionnaires.models import QuestionnaireEvaluation, QuestionnaireSubmission, SubmissionSourceEventMetadata
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
