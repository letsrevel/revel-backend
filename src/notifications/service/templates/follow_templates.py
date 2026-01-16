"""Templates for follow-related notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class OrganizationFollowedTemplate(NotificationTemplate):
    """Template for ORGANIZATION_FOLLOWED notification (to org admins)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        follower_name = notification.context.get("follower_name", "Someone")
        organization_name = notification.context.get("organization_name", "your organization")
        return _("%(follower)s started following %(org)s") % {
            "follower": follower_name,
            "org": organization_name,
        }

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        organization_name = notification.context.get("organization_name", "your organization")
        return _("New follower - %(org)s") % {"org": organization_name}


class EventSeriesFollowedTemplate(NotificationTemplate):
    """Template for EVENT_SERIES_FOLLOWED notification (to org admins)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        follower_name = notification.context.get("follower_name", "Someone")
        series_name = notification.context.get("event_series_name", "your series")
        return _("%(follower)s started following %(series)s") % {
            "follower": follower_name,
            "series": series_name,
        }

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        series_name = notification.context.get("event_series_name", "your series")
        return _("New follower - %(series)s") % {"series": series_name}


class NewEventFromFollowedOrgTemplate(NotificationTemplate):
    """Template for NEW_EVENT_FROM_FOLLOWED_ORG notification (to followers)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        organization_name = notification.context.get("organization_name", "An organization you follow")
        event_name = notification.context.get("event_name", "a new event")
        return _("%(org)s created %(event)s") % {
            "org": organization_name,
            "event": event_name,
        }

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        organization_name = notification.context.get("organization_name", "An organization you follow")
        return _("New event from %(org)s") % {"org": organization_name}


class NewEventFromFollowedSeriesTemplate(NotificationTemplate):
    """Template for NEW_EVENT_FROM_FOLLOWED_SERIES notification (to followers)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        series_name = notification.context.get("event_series_name", "A series you follow")
        event_name = notification.context.get("event_name", "a new event")
        return _("New event in %(series)s: %(event)s") % {
            "series": series_name,
            "event": event_name,
        }

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        series_name = notification.context.get("event_series_name", "A series you follow")
        return _("New event in %(series)s") % {"series": series_name}


# Register templates
register_template(NotificationType.ORGANIZATION_FOLLOWED, OrganizationFollowedTemplate())
register_template(NotificationType.EVENT_SERIES_FOLLOWED, EventSeriesFollowedTemplate())
register_template(NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG, NewEventFromFollowedOrgTemplate())
register_template(NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES, NewEventFromFollowedSeriesTemplate())
