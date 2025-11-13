"""Templates for invitation-related notifications."""

from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class InvitationReceivedTemplate(NotificationTemplate):
    """Template for INVITATION_RECEIVED notification."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        invited_by = notification.context.get("invited_by_name", "")
        event_name = notification.context.get("event_name", "")
        return _("%(inviter)s invited you to %(event)s") % {
            "inviter": invited_by,
            "event": event_name,
        }

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        invited_by = ctx.get("invited_by_name", "")
        event_name = ctx.get("event_name", "")
        event_start = ctx.get("event_start", "")
        personal_message = ctx.get("personal_message")

        body = _("**%(inviter)s** has invited you to **%(event)s** on %(date)s.") % {
            "inviter": invited_by,
            "event": event_name,
            "date": event_start,
        }

        if personal_message:
            body += "\n\n" + _("Personal message: %(message)s") % {"message": personal_message}

        return body

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("You're invited: %(event)s") % {"event": event_name}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/invitation_received.txt",
            {"user": notification.user, "context": notification.context},
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/invitation_received.html",
            {"user": notification.user, "context": notification.context},
        )


class InvitationClaimedTemplate(NotificationTemplate):
    """Template for INVITATION_CLAIMED notification (to organizers)."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        claimed_by = notification.context.get("claimed_by_name", "")
        event_name = notification.context.get("event_name", "")
        return _("%(user)s claimed invitation to %(event)s") % {
            "user": claimed_by,
            "event": event_name,
        }

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        claimed_by = ctx.get("claimed_by_name", "")
        event_name = ctx.get("event_name", "")
        return _("**%(user)s** has claimed their invitation to **%(event)s**.") % {
            "user": claimed_by,
            "event": event_name,
        }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        claimed_by = notification.context.get("claimed_by_name", "")
        event_name = notification.context.get("event_name", "")
        return _("Invitation claimed: %(user)s - %(event)s") % {
            "user": claimed_by,
            "event": event_name,
        }

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/invitation_claimed.txt",
            {"user": notification.user, "context": notification.context},
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/invitation_claimed.html",
            {"user": notification.user, "context": notification.context},
        )


# Register templates
register_template(NotificationType.INVITATION_RECEIVED, InvitationReceivedTemplate())
register_template(NotificationType.INVITATION_CLAIMED, InvitationClaimedTemplate())
