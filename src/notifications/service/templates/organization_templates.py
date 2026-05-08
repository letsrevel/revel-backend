"""Templates for organization-related notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class OrgAnnouncementTemplate(NotificationTemplate):
    """Template for ORG_ANNOUNCEMENT notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        announcement_title = notification.context.get("announcement_title", "")
        return _("%(org)s: %(title)s") % {"org": org_name, "title": announcement_title}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        announcement_title = notification.context.get("announcement_title", "")
        return _("%(org)s - %(title)s") % {"org": org_name, "title": announcement_title}


class OrgContactMessageReceivedTemplate(NotificationTemplate):
    """Template for ORG_CONTACT_MESSAGE_RECEIVED notification (to org admins)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        sender_email = notification.context.get("sender_email", "")
        return _("New contact message for %(org)s from %(sender)s") % {"org": org_name, "sender": sender_email}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject (only used if a user opts EMAIL channel in)."""
        org_name = notification.context.get("organization_name", "")
        return _("New contact message: %(org)s") % {"org": org_name}


# Register templates
register_template(NotificationType.ORG_ANNOUNCEMENT, OrgAnnouncementTemplate())
register_template(NotificationType.ORG_CONTACT_MESSAGE_RECEIVED, OrgContactMessageReceivedTemplate())
