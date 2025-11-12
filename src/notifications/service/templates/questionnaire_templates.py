"""Templates for questionnaire-related notifications."""

from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class QuestionnaireSubmittedTemplate(NotificationTemplate):
    """Template for QUESTIONNAIRE_SUBMITTED notification (to staff)."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        ctx = notification.context
        return _("New Questionnaire Submission: %(name)s") % {"name": ctx.get("questionnaire_name", "")}

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        return _("**%(submitter)s** submitted the %(questionnaire)s questionnaire for review.") % {
            "submitter": ctx.get("submitter_name", ""),
            "questionnaire": ctx.get("questionnaire_name", ""),
        }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        ctx = notification.context
        return _("New Questionnaire Submission - %(name)s") % {"name": ctx.get("questionnaire_name", "")}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/questionnaire_submitted.txt",
            {"user": notification.user, "context": notification.context},
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/questionnaire_submitted.html",
            {"user": notification.user, "context": notification.context},
        )


class QuestionnaireEvaluationTemplate(NotificationTemplate):
    """Template for QUESTIONNAIRE_EVALUATION_RESULT notification (to user)."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        ctx = notification.context
        status = ctx.get("evaluation_status", "")
        questionnaire_name = ctx.get("questionnaire_name", "")

        if status == "APPROVED":
            return _("✅ Questionnaire Approved: %(name)s") % {"name": questionnaire_name}
        elif status == "REJECTED":
            return _("❌ Questionnaire Not Approved: %(name)s") % {"name": questionnaire_name}
        else:
            return _("Questionnaire Evaluation: %(name)s") % {"name": questionnaire_name}

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        status = ctx.get("evaluation_status", "")
        questionnaire_name = ctx.get("questionnaire_name", "")

        if status == "APPROVED":
            return _("Your **%(name)s** questionnaire has been approved!") % {"name": questionnaire_name}
        elif status == "REJECTED":
            return _("Your **%(name)s** questionnaire was not approved.") % {"name": questionnaire_name}
        else:
            return _("Your **%(name)s** questionnaire has been evaluated.") % {"name": questionnaire_name}

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        ctx = notification.context
        return _("Questionnaire Evaluation Result - %(name)s") % {"name": ctx.get("questionnaire_name", "")}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/questionnaire_evaluation.txt",
            {"user": notification.user, "context": notification.context},
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/questionnaire_evaluation.html",
            {"user": notification.user, "context": notification.context},
        )


# Register templates
register_template(NotificationType.QUESTIONNAIRE_SUBMITTED, QuestionnaireSubmittedTemplate())
register_template(NotificationType.QUESTIONNAIRE_EVALUATION_RESULT, QuestionnaireEvaluationTemplate())
