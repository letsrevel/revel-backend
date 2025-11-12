"""Templates for potluck-related notifications."""

from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class PotluckItemTemplate(NotificationTemplate):
    """Template for potluck item notifications (created, updated, claimed, unclaimed)."""

    def get_title(self, notification: Notification) -> str:
        """Get title."""
        action = notification.context.get("action", "")
        item_name = notification.context.get("item_name", "")

        if action == "created":
            return _("New Potluck Item: %(item)s") % {"item": item_name}
        elif action == "claimed":
            return _("Potluck Item Claimed: %(item)s") % {"item": item_name}
        elif action == "unclaimed":
            return _("Potluck Item Available: %(item)s") % {"item": item_name}
        else:
            return _("Potluck Update: %(item)s") % {"item": item_name}

    def get_body(self, notification: Notification) -> str:
        """Get body."""
        ctx = notification.context
        action = ctx.get("action", "")
        item_name = ctx.get("item_name", "")
        event_name = ctx.get("event_name", "")

        if action == "created":
            return _("New potluck item added to %(event)s: **%(item)s**") % {
                "event": event_name,
                "item": item_name,
            }
        elif action == "claimed":
            assigned_to = ctx.get("assigned_to_username", _("someone"))
            return _("%(item)s has been claimed by %(user)s for %(event)s") % {
                "item": item_name,
                "user": assigned_to,
                "event": event_name,
            }
        elif action == "unclaimed":
            return _("%(item)s is now available again for %(event)s") % {
                "item": item_name,
                "event": event_name,
            }
        else:
            return _("Potluck item updated for %(event)s: %(item)s") % {
                "event": event_name,
                "item": item_name,
            }

    def get_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Potluck Update - %(event)s") % {"event": event_name}

    def get_text_body(self, notification: Notification) -> str:
        """Get email text body."""
        # For now, we'll use the old template by creating a fake context
        # In the future, we should copy and update the template
        return render_to_string(
            "notifications/emails/potluck_update.txt",
            {"user": notification.user, "context": notification.context},
        )

    def get_html_body(self, notification: Notification) -> str:
        """Get email HTML body."""
        return render_to_string(
            "notifications/emails/potluck_update.html",
            {"user": notification.user, "context": notification.context},
        )


# Register templates
register_template(NotificationType.POTLUCK_ITEM_CREATED, PotluckItemTemplate())
register_template(NotificationType.POTLUCK_ITEM_UPDATED, PotluckItemTemplate())
register_template(NotificationType.POTLUCK_ITEM_CLAIMED, PotluckItemTemplate())
register_template(NotificationType.POTLUCK_ITEM_UNCLAIMED, PotluckItemTemplate())
