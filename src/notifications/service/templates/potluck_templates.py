"""Templates for potluck-related notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class PotluckItemTemplate(NotificationTemplate):
    """Template for potluck item notifications (created, updated, claimed, unclaimed)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        action = notification.context.get("action", "")
        item_name = notification.context.get("item_name", "")
        actor_name = notification.context.get("actor_name")
        is_organizer = notification.context.get("is_organizer", False)

        # Organizers get actor-focused titles
        if is_organizer and actor_name:
            if action == "created":
                return _("%(actor)s added %(item)s") % {"actor": actor_name, "item": item_name}
            if action == "created_and_claimed":
                return _("%(actor)s added and claimed %(item)s") % {"actor": actor_name, "item": item_name}
            if action == "claimed":
                return _("%(actor)s claimed %(item)s") % {"actor": actor_name, "item": item_name}

        # Regular participants get item-focused titles
        if action == "created":
            return _("New Potluck Item: %(item)s") % {"item": item_name}
        if action == "created_and_claimed":
            return _("New Potluck Item Claimed: %(item)s") % {"item": item_name}
        if action == "claimed":
            return _("Potluck Item Claimed: %(item)s") % {"item": item_name}
        if action == "unclaimed":
            return _("Potluck Item Available: %(item)s") % {"item": item_name}
        if action == "deleted":
            return _("Potluck Item Removed: %(item)s") % {"item": item_name}
        return _("Potluck Update: %(item)s") % {"item": item_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        action = notification.context.get("action", "")
        item_name = notification.context.get("item_name", "")
        event_name = notification.context.get("event_name", "")
        actor_name = notification.context.get("actor_name")
        is_organizer = notification.context.get("is_organizer", False)

        # Organizers get detailed subjects with actor
        if is_organizer and actor_name:
            if action == "created":
                return _("%(actor)s added %(item)s - %(event)s") % {
                    "actor": actor_name,
                    "item": item_name,
                    "event": event_name,
                }
            if action == "created_and_claimed":
                return _("%(actor)s added and claimed %(item)s - %(event)s") % {
                    "actor": actor_name,
                    "item": item_name,
                    "event": event_name,
                }
            if action == "claimed":
                return _("%(actor)s claimed %(item)s - %(event)s") % {
                    "actor": actor_name,
                    "item": item_name,
                    "event": event_name,
                }

        # Handle deleted action specifically
        if action == "deleted":
            return _("Potluck Item Removed - %(event)s") % {"event": event_name}

        # Regular participants get simple subject
        return _("Potluck Update - %(event)s") % {"event": event_name}


# Register templates
register_template(NotificationType.POTLUCK_ITEM_CREATED, PotluckItemTemplate())
register_template(NotificationType.POTLUCK_ITEM_CREATED_AND_CLAIMED, PotluckItemTemplate())
register_template(NotificationType.POTLUCK_ITEM_UPDATED, PotluckItemTemplate())
register_template(NotificationType.POTLUCK_ITEM_CLAIMED, PotluckItemTemplate())
register_template(NotificationType.POTLUCK_ITEM_UNCLAIMED, PotluckItemTemplate())
register_template(NotificationType.POTLUCK_ITEM_DELETED, PotluckItemTemplate())
