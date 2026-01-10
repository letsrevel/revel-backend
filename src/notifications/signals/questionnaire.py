"""Signal handlers for questionnaire notifications."""

import typing as t

import structlog
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from common.models import SiteSettings
from notifications.enums import NotificationType
from notifications.service.eligibility import get_staff_for_notification
from notifications.signals import notification_requested
from questionnaires.models import QuestionnaireEvaluation, QuestionnaireSubmission

logger = structlog.get_logger(__name__)


@receiver(post_save, sender=QuestionnaireSubmission)
def handle_questionnaire_submission(
    sender: type[QuestionnaireSubmission], instance: QuestionnaireSubmission, created: bool, **kwargs: t.Any
) -> None:
    """Send notifications when a questionnaire is submitted.

    Only sends notifications for event-related questionnaires.
    Notifies organization staff and owners.
    """
    if not created:
        return  # Only notify on creation

    # Get organization data to determine if this is event-related
    # Only send notifications for questionnaires linked to organizations
    from events.models import OrganizationQuestionnaire

    org_data = (
        OrganizationQuestionnaire.objects.filter(questionnaire_id=instance.questionnaire_id)
        .select_related("organization")
        .values_list("id", "organization_id", "organization__name", "organization__slug")
        .first()
    )

    if not org_data:
        # No organization linked, skip notifications
        logger.debug(
            "questionnaire_submission_no_org",
            submission_id=str(instance.id),
            questionnaire_id=str(instance.questionnaire_id),
        )
        return

    org_questionnaire_id, organization_id, organization_name, organization_slug = org_data

    # Build submission URL for admin view
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    submission_url = (
        f"{frontend_base_url}/org/{organization_slug}/admin/questionnaires/"
        f"{org_questionnaire_id}/submissions/{instance.id}"
    )

    # Get staff and owners with evaluate_questionnaire permission
    staff_and_owners = get_staff_for_notification(organization_id, NotificationType.QUESTIONNAIRE_SUBMITTED)

    # Build base context
    context: dict[str, t.Any] = {
        "submission_id": str(instance.id),
        "questionnaire_name": instance.questionnaire.name,
        "submitter_email": instance.user.email,
        "submitter_name": instance.user.get_display_name(),
        "organization_id": str(organization_id),
        "organization_name": organization_name,
        "submission_url": submission_url,
    }

    # Add event info from submission metadata if available
    source_event = instance.source_event
    if source_event:
        context["event_id"] = source_event["event_id"]
        context["event_name"] = source_event["event_name"]

    # Send notification to all eligible users
    for staff_user in staff_and_owners:
        notification_requested.send(
            sender=sender,
            user=staff_user,
            notification_type=NotificationType.QUESTIONNAIRE_SUBMITTED,
            context=context,
        )

    logger.info(
        "questionnaire_submission_notifications_sent",
        submission_id=str(instance.id),
        organization_id=str(organization_id),
        recipients_count=len(list(staff_and_owners)),
    )


@receiver(pre_save, sender=QuestionnaireEvaluation)
def capture_evaluation_old_status(
    sender: type[QuestionnaireEvaluation], instance: QuestionnaireEvaluation, **kwargs: t.Any
) -> None:
    """Capture the old status value before save for change detection."""
    if instance.pk:
        try:
            old_instance = QuestionnaireEvaluation.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                instance._old_status = old_instance.status  # type: ignore[attr-defined]
        except QuestionnaireEvaluation.DoesNotExist:
            pass


@receiver(post_save, sender=QuestionnaireEvaluation)
def handle_questionnaire_evaluation(
    sender: type[QuestionnaireEvaluation], instance: QuestionnaireEvaluation, created: bool, **kwargs: t.Any
) -> None:
    """Send notifications when a questionnaire evaluation status changes to approved/rejected.

    Only sends notifications when status changes to APPROVED or REJECTED.
    Notifies the user who submitted the questionnaire.
    """
    # Check if this is a status change to approved/rejected
    if not hasattr(instance, "_old_status"):
        # No status change (either new or status didn't change)
        if not created:
            return

        # New evaluation - only notify if already approved/rejected
        if instance.status not in [
            QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
            QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        ]:
            return
    else:
        # Status changed - only notify for approved/rejected states
        if instance.status not in [
            QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
            QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        ]:
            return

    # Get organization questionnaire with related data
    from events.models import OrganizationQuestionnaire

    org_questionnaire = (
        OrganizationQuestionnaire.objects.filter(questionnaire_id=instance.submission.questionnaire_id)
        .select_related("organization")
        .prefetch_related("events")
        .first()
    )

    if not org_questionnaire:
        logger.debug(
            "questionnaire_evaluation_no_org",
            evaluation_id=str(instance.id),
            submission_id=str(instance.submission_id),
        )
        return

    # Build context
    context: dict[str, t.Any] = {
        "submission_id": str(instance.submission_id),
        "questionnaire_name": instance.submission.questionnaire.name,
        "evaluation_status": instance.status.upper(),
        "evaluation_score": str(instance.score) if instance.score else None,
        "evaluation_comments": instance.comments,
        "organization_name": org_questionnaire.organization.name,
    }

    # Add event info from submission metadata (source_event) if available
    # This captures the exact event context where the questionnaire was submitted
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    source_event = instance.submission.source_event
    if source_event:
        context["event_id"] = source_event["event_id"]
        context["event_name"] = source_event["event_name"]
        context["event_url"] = f"{frontend_base_url}/events/{source_event['event_id']}"
    else:
        # Fallback: Get the first linked event from org_questionnaire
        # (for submissions created before metadata was added)
        first_event = org_questionnaire.events.first()
        if first_event:
            context["event_id"] = str(first_event.id)
            context["event_name"] = first_event.name
            context["event_url"] = f"{frontend_base_url}/events/{first_event.id}"

    # Send notification to the submitter
    notification_requested.send(
        sender=sender,
        user=instance.submission.user,
        notification_type=NotificationType.QUESTIONNAIRE_EVALUATION_RESULT,
        context=context,
    )

    logger.info(
        "questionnaire_evaluation_notification_sent",
        evaluation_id=str(instance.id),
        submission_id=str(instance.submission_id),
        status=instance.status,
        user_id=str(instance.submission.user_id),
    )
