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


# Register templates
register_template(NotificationType.ORG_ANNOUNCEMENT, OrgAnnouncementTemplate())
