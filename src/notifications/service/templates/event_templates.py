"""Templates for event-related notifications."""

from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class EventOpenTemplate(NotificationTemplate):
    """Template for EVENT_OPEN notification."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        event_name = notification.context.get("event_name", "")
        return _("New Event: %(event)s") % {"event": event_name}

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        org_name = ctx.get("organization_name", "")
        event_name = ctx.get("event_name", "")
        return _("%(organization)s has published a new event: **%(event)s**") % {
            "organization": org_name,
            "event": event_name,
        }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("New Event: %(event)s") % {"event": event_name}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/event_open.txt", {"user": notification.user, "context": notification.context}
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/event_open.html", {"user": notification.user, "context": notification.context}
        )


class EventReminderTemplate(NotificationTemplate):
    """Template for EVENT_REMINDER notification."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        days = notification.context.get("days_until", 0)
        event_name = notification.context.get("event_name", "")

        if days == 1:
            return _("Reminder: %(event)s is tomorrow!") % {"event": event_name}
        else:
            return _("Reminder: %(event)s in %(days)d days") % {"event": event_name, "days": days}

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        days = ctx.get("days_until", 0)
        event_name = ctx.get("event_name", "")
        event_start = ctx.get("event_start", "")

        if days == 1:
            return _("**%(event)s** is tomorrow at %(time)s. Don't forget!") % {
                "event": event_name,
                "time": event_start,
            }
        else:
            return _("**%(event)s** is in %(days)d days (%(time)s). Mark your calendar!") % {
                "event": event_name,
                "days": days,
                "time": event_start,
            }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        days = notification.context.get("days_until", 0)
        event_name = notification.context.get("event_name", "")

        if days == 1:
            return _("Tomorrow: %(event)s") % {"event": event_name}
        else:
            return _("Reminder: %(event)s in %(days)d days") % {"event": event_name, "days": days}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/event_reminder.txt", {"user": notification.user, "context": notification.context}
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/event_reminder.html", {"user": notification.user, "context": notification.context}
        )


# Register templates
register_template(NotificationType.EVENT_OPEN, EventOpenTemplate())
register_template(NotificationType.EVENT_REMINDER, EventReminderTemplate())
