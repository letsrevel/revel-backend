"""Templates for RSVP-related notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class RSVPConfirmationTemplate(NotificationTemplate):
    """Template for RSVP_CONFIRMATION notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        ctx = notification.context
        event_name = ctx.get("event_name", "")
        response = ctx.get("response", "").upper()
        user_name = ctx.get("user_name")

        if user_name:
            # Staff notification: include user name
            return _("RSVP Confirmed: %(event)s (%(user)s: %(response)s)") % {
                "event": event_name,
                "user": user_name,
                "response": response,
            }
        return _("RSVP Confirmed: %(event)s (%(response)s)") % {"event": event_name, "response": response}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        ctx = notification.context
        event_name = ctx.get("event_name", "")
        user_name = ctx.get("user_name")

        if user_name:
            return _("RSVP Confirmed: %(event)s (%(user)s)") % {"event": event_name, "user": user_name}
        return _("RSVP Confirmed: %(event)s") % {"event": event_name}


class RSVPUpdatedTemplate(NotificationTemplate):
    """Template for RSVP_UPDATED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        ctx = notification.context
        event_name = ctx.get("event_name", "")
        new_response = ctx.get("new_response", "").upper()
        user_name = ctx.get("user_name")

        if user_name:
            # Staff notification: include user name
            return _("RSVP Updated: %(event)s (%(user)s: %(response)s)") % {
                "event": event_name,
                "user": user_name,
                "response": new_response,
            }
        return _("RSVP Updated: %(event)s (%(response)s)") % {"event": event_name, "response": new_response}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        ctx = notification.context
        event_name = ctx.get("event_name", "")
        user_name = ctx.get("user_name")
        new_response = ctx.get("new_response", "").upper()

        if user_name:
            return _("RSVP Updated: %(event)s (%(user)s: %(new_response)s)") % {
                "event": event_name,
                "user": user_name,
                "new_response": new_response,
            }
        return _("RSVP Updated: %(event)s") % {"event": event_name}


class RSVPCancelledTemplate(NotificationTemplate):
    """Template for RSVP_CANCELLED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        return _("RSVP Cancelled: %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("RSVP Cancelled: %(event)s") % {"event": event_name}


# Register templates
register_template(NotificationType.RSVP_CONFIRMATION, RSVPConfirmationTemplate())
register_template(NotificationType.RSVP_UPDATED, RSVPUpdatedTemplate())
register_template(NotificationType.RSVP_CANCELLED, RSVPCancelledTemplate())
