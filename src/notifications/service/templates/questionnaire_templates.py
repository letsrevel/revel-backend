"""Templates for questionnaire-related notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class QuestionnaireSubmittedTemplate(NotificationTemplate):
    """Template for QUESTIONNAIRE_SUBMITTED notification (to staff)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title."""
        ctx = notification.context
        return _("New Questionnaire Submission: %(name)s") % {"name": ctx.get("questionnaire_name", "")}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        ctx = notification.context
        return _("New Questionnaire Submission - %(name)s") % {"name": ctx.get("questionnaire_name", "")}


class QuestionnaireEvaluationTemplate(NotificationTemplate):
    """Template for QUESTIONNAIRE_EVALUATION_RESULT notification (to user)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        ctx = notification.context
        status = ctx.get("evaluation_status", "")
        questionnaire_name = ctx.get("questionnaire_name", "")

        if status == "APPROVED":
            return _("✅ Questionnaire Approved: %(name)s") % {"name": questionnaire_name}
        if status == "REJECTED":
            return _("❌ Questionnaire Not Approved: %(name)s") % {"name": questionnaire_name}
        return _("Questionnaire Evaluation: %(name)s") % {"name": questionnaire_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        ctx = notification.context
        return _("Questionnaire Evaluation Result - %(name)s") % {"name": ctx.get("questionnaire_name", "")}


# Register templates
register_template(NotificationType.QUESTIONNAIRE_SUBMITTED, QuestionnaireSubmittedTemplate())
register_template(NotificationType.QUESTIONNAIRE_EVALUATION_RESULT, QuestionnaireEvaluationTemplate())
