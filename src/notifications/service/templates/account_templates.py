"""Templates for account-level notifications."""

from django.utils.translation import gettext as _

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class AccountBannedTemplate(NotificationTemplate):
    """Template for ACCOUNT_BANNED notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        return str(_("Account Suspended"))

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        return str(_("Revel - Your account has been suspended"))


# Register templates
register_template(NotificationType.ACCOUNT_BANNED, AccountBannedTemplate())
