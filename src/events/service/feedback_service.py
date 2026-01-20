"""Feedback questionnaire service.

This module provides validation and utility functions for feedback questionnaires.
Feedback questionnaires are only accessible after an event has ended and only
to users who attended the event.
"""

from __future__ import annotations

from uuid import UUID

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventQuestionnaireSubmission, EventRSVP, OrganizationQuestionnaire, Ticket
from questionnaires.models import Questionnaire


def user_attended_event(user: RevelUser, event: Event) -> bool:
    """Check if a user attended an event.

    A user is considered to have attended if they:
    - Have an RSVP with status YES, or
    - Have an ACTIVE or CHECKED_IN ticket

    Args:
        user: The user to check.
        event: The event to check attendance for.

    Returns:
        True if user attended, False otherwise.
    """
    # Check for YES RSVP
    has_rsvp = EventRSVP.objects.filter(
        event=event,
        user=user,
        status=EventRSVP.RsvpStatus.YES,
    ).exists()

    if has_rsvp:
        return True

    # Check for active/checked-in ticket
    has_ticket = Ticket.objects.filter(
        event=event,
        user=user,
        status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN],
    ).exists()

    return has_ticket


def user_already_submitted_feedback(
    user: RevelUser,
    event: Event,
    questionnaire: Questionnaire,
) -> bool:
    """Check if user already submitted feedback for this event and questionnaire.

    Args:
        user: The user to check.
        event: The event to check.
        questionnaire: The questionnaire to check.

    Returns:
        True if user already submitted feedback, False otherwise.
    """
    return EventQuestionnaireSubmission.objects.filter(
        user=user,
        event=event,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
    ).exists()


def validate_feedback_questionnaire_access(
    user: RevelUser,
    event: Event,
    org_questionnaire: OrganizationQuestionnaire,
    *,
    check_already_submitted: bool = True,
) -> None:
    """Validate that a user can access a feedback questionnaire.

    For FEEDBACK type questionnaires, validates that:
    1. The event has ended (timezone.now() > event.end)
    2. The user attended the event (YES RSVP or active/checked-in ticket)
    3. The user hasn't already submitted feedback (optional)

    For non-FEEDBACK questionnaires, this function does nothing.

    Args:
        user: The user attempting to access the questionnaire.
        event: The event the questionnaire is associated with.
        org_questionnaire: The organization questionnaire to validate.
        check_already_submitted: Whether to check if user already submitted.

    Returns:
        None. Raises HttpError on validation failure.

    Raises:
        HttpError(403): If the event hasn't ended yet.
        HttpError(403): If the user didn't attend the event.
        HttpError(403): If the user already submitted feedback.
    """
    if org_questionnaire.questionnaire_type != OrganizationQuestionnaire.QuestionnaireType.FEEDBACK:
        return

    # Check event has ended
    if event.end is None or timezone.now() <= event.end:
        raise HttpError(403, str(_("Feedback questionnaire is only available after the event ends.")))

    # Check user attended
    if not user_attended_event(user, event):
        raise HttpError(403, str(_("You can only submit feedback for events you attended.")))

    # Check user hasn't already submitted
    if check_already_submitted and user_already_submitted_feedback(user, event, org_questionnaire.questionnaire):
        raise HttpError(403, str(_("You have already submitted feedback for this event.")))


def get_feedback_questionnaires_for_user(
    event: Event,
    user: RevelUser,
    *,
    attendance_verified: bool = False,
) -> list[UUID]:
    """Get feedback questionnaire IDs available for a user for a given event.

    Returns feedback questionnaires that:
    1. Are linked to the event (directly or via event series)
    2. The event has ended
    3. The user attended the event
    4. The user hasn't already submitted feedback

    Args:
        event: The event to get feedback questionnaires for.
        user: The user to check eligibility for.
        attendance_verified: If True, skip the attendance check. Use this when
            the caller has already verified attendance to avoid redundant queries.

    Returns:
        List of questionnaire IDs the user can provide feedback for.
    """
    # Event must have ended
    if event.end is None or timezone.now() <= event.end:
        return []

    # User must have attended (skip if caller already verified)
    if not attendance_verified and not user_attended_event(user, event):
        return []

    # Build filter for questionnaires linked to this event
    filter_q = Q(events=event)
    if event.event_series_id:
        filter_q |= Q(event_series=event.event_series_id)

    # Get questionnaire IDs user has already submitted feedback for this event
    already_submitted = EventQuestionnaireSubmission.objects.filter(
        user=user,
        event=event,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
    ).values_list("questionnaire_id", flat=True)

    # Get FEEDBACK questionnaires for this event, excluding already submitted
    feedback_questionnaires = (
        OrganizationQuestionnaire.objects.filter(
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        .filter(filter_q)
        .exclude(questionnaire_id__in=already_submitted)
    )

    return list(feedback_questionnaires.values_list("questionnaire_id", flat=True))


def validate_not_feedback_questionnaire_for_evaluation(
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Validate that a questionnaire is not a FEEDBACK type for evaluation purposes.

    Feedback questionnaires should not be evaluated (approved/rejected).
    The only exception is if an admin changes the type from FEEDBACK to something else,
    in which case existing submissions would become evaluatable.

    Args:
        org_questionnaire: The organization questionnaire to validate.

    Raises:
        HttpError(400): If the questionnaire is a FEEDBACK type.
    """
    if org_questionnaire.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.FEEDBACK:
        raise HttpError(400, str(_("Feedback questionnaires cannot be evaluated.")))
