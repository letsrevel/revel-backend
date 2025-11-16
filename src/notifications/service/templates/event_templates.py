"""Templates for event-related notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class EventOpenTemplate(NotificationTemplate):
    """Template for EVENT_OPEN notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        return _("New Event: %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("%(org)s has published a new event") % {"org": org_name}


class EventReminderTemplate(NotificationTemplate):
    """Template for EVENT_REMINDER notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        days = notification.context.get("days_until", 0)
        event_name = notification.context.get("event_name", "")

        if days == 1:
            return _("Reminder: %(event)s is tomorrow!") % {"event": event_name}
        return _("Reminder: %(event)s in %(days)d days") % {"event": event_name, "days": days}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        days = notification.context.get("days_until", 0)
        event_name = notification.context.get("event_name", "")

        if days == 1:
            return _("Tomorrow: %(event)s") % {"event": event_name}
        return _("Reminder: %(event)s in %(days)d days") % {"event": event_name, "days": days}


class EventUpdatedTemplate(NotificationTemplate):
    """Template for EVENT_UPDATED notification (to attendees)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        return _("Event Updated: %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Important Update: %(event)s") % {"event": event_name}


class EventCancelledTemplate(NotificationTemplate):
    """Template for EVENT_CANCELLED notification (to attendees)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        event_name = notification.context.get("event_name", "")
        return _("Event Cancelled: %(event)s") % {"event": event_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Event Cancelled: %(event)s") % {"event": event_name}


# Register templates
register_template(NotificationType.EVENT_OPEN, EventOpenTemplate())
register_template(NotificationType.EVENT_REMINDER, EventReminderTemplate())
register_template(NotificationType.EVENT_UPDATED, EventUpdatedTemplate())
register_template(NotificationType.EVENT_CANCELLED, EventCancelledTemplate())
