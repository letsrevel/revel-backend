"""Event questionnaire submission service.

This module provides functions for submitting questionnaires through the events
endpoint with atomic tracking. It handles creating both the QuestionnaireSubmission
and EventQuestionnaireSubmission records in a single transaction.
"""

from __future__ import annotations

import typing as t

from django.db import transaction
from django.db.models import Q

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from events.models import Event, EventQuestionnaireSubmission, OrganizationQuestionnaire
from questionnaires.models import QuestionnaireSubmission, SubmissionSourceEventMetadata
from questionnaires.schema import QuestionnaireSubmissionSchema

if t.TYPE_CHECKING:
    from questionnaires.service.questionnaire_service import QuestionnaireService


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
    """
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
