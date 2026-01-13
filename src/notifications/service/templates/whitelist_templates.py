"""Templates for whitelist-related notifications (blacklist verification flow)."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class WhitelistRequestCreatedTemplate(NotificationTemplate):
    """Template for WHITELIST_REQUEST_CREATED notification (to organizers)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        requester_name = notification.context.get("requester_name", "")
        org_name = notification.context.get("organization_name", "")
        return _("%(user)s requested verification for %(org)s") % {"user": requester_name, "org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("New verification request: %(org)s") % {"org": org_name}


class WhitelistRequestApprovedTemplate(NotificationTemplate):
    """Template for WHITELIST_REQUEST_APPROVED notification (to user)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Verification Approved: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Verification Approved - %(org)s") % {"org": org_name}


class WhitelistRequestRejectedTemplate(NotificationTemplate):
    """Template for WHITELIST_REQUEST_REJECTED notification (to user)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        org_name = notification.context.get("organization_name", "")
        return _("Verification Declined: %(org)s") % {"org": org_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        org_name = notification.context.get("organization_name", "")
        return _("Verification Update: %(org)s") % {"org": org_name}


# Register templates
register_template(NotificationType.WHITELIST_REQUEST_CREATED, WhitelistRequestCreatedTemplate())
register_template(NotificationType.WHITELIST_REQUEST_APPROVED, WhitelistRequestApprovedTemplate())
register_template(NotificationType.WHITELIST_REQUEST_REJECTED, WhitelistRequestRejectedTemplate())
