"""Templates for RSVP-related notifications."""

from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class RSVPConfirmationTemplate(NotificationTemplate):
    """Template for RSVP_CONFIRMATION notification."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
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

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        event_name = ctx.get("event_name", "")
        response = ctx.get("response", "").upper()
        user_name = ctx.get("user_name")

        if user_name:
            # Staff notification: use third person
            return _("**%(user)s**'s RSVP for **%(event)s** has been confirmed as **%(response)s**.") % {
                "user": user_name,
                "event": event_name,
                "response": response,
            }
        return _("Your RSVP for **%(event)s** has been confirmed as **%(response)s**.") % {
            "event": event_name,
            "response": response,
        }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        ctx = notification.context
        event_name = ctx.get("event_name", "")
        user_name = ctx.get("user_name")

        if user_name:
            return _("RSVP Confirmed: %(event)s (%(user)s)") % {"event": event_name, "user": user_name}
        return _("RSVP Confirmed: %(event)s") % {"event": event_name}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/rsvp_confirmation.txt", {"user": notification.user, "context": notification.context}
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/rsvp_confirmation.html", {"user": notification.user, "context": notification.context}
        )


class RSVPUpdatedTemplate(NotificationTemplate):
    """Template for RSVP_UPDATED notification."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
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

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        event_name = ctx.get("event_name", "")
        new_response = ctx.get("new_response", "").upper()
        user_name = ctx.get("user_name")

        if user_name:
            # Staff notification: use third person
            return _("**%(user)s**'s RSVP for **%(event)s** has been updated to **%(response)s**.") % {
                "user": user_name,
                "event": event_name,
                "response": new_response,
            }
        return _("Your RSVP for **%(event)s** has been updated to **%(response)s**.") % {
            "event": event_name,
            "response": new_response,
        }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        ctx = notification.context
        event_name = ctx.get("event_name", "")
        user_name = ctx.get("user_name")

        if user_name:
            return _("RSVP Updated: %(event)s (%(user)s)") % {"event": event_name, "user": user_name}
        return _("RSVP Updated: %(event)s") % {"event": event_name}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/rsvp_updated.txt", {"user": notification.user, "context": notification.context}
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/rsvp_updated.html", {"user": notification.user, "context": notification.context}
        )


class RSVPCancelledTemplate(NotificationTemplate):
    """Template for RSVP_CANCELLED notification."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        event_name = notification.context.get("event_name", "")
        return _("RSVP Cancelled: %(event)s") % {"event": event_name}

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        event_name = ctx.get("event_name", "")
        user_name = ctx.get("user_name", "A user")
        return _("%(user)s has cancelled their RSVP for **%(event)s**.") % {
            "user": user_name,
            "event": event_name,
        }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("RSVP Cancelled: %(event)s") % {"event": event_name}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        return render_to_string(
            "notifications/emails/rsvp_cancelled.txt", {"user": notification.user, "context": notification.context}
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/rsvp_cancelled.html", {"user": notification.user, "context": notification.context}
        )


# Register templates
register_template(NotificationType.RSVP_CONFIRMATION, RSVPConfirmationTemplate())
register_template(NotificationType.RSVP_UPDATED, RSVPUpdatedTemplate())
register_template(NotificationType.RSVP_CANCELLED, RSVPCancelledTemplate())
