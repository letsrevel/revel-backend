"""Templates for invitation-related notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class InvitationReceivedTemplate(NotificationTemplate):
    """Template for INVITATION_RECEIVED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        return _("You're invited to %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("You're invited: %(event)s") % {"event": event_name}


class InvitationRequestCreatedTemplate(NotificationTemplate):
    """Template for INVITATION_REQUEST_CREATED notification (to organizers)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        requester_email = notification.context.get("requester_email", "")
        event_name = notification.context.get("event_name", "")
        return _("%(email)s requested invitation to %(event)s") % {
            "email": requester_email,
            "event": event_name,
        }

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("New invitation request: %(event)s") % {"event": event_name}


# Register templates
register_template(NotificationType.INVITATION_RECEIVED, InvitationReceivedTemplate())
register_template(NotificationType.INVITATION_REQUEST_CREATED, InvitationRequestCreatedTemplate())
