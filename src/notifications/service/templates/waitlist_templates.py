"""Templates for waitlist-related notifications."""

from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class WaitlistSpotAvailableTemplate(NotificationTemplate):
    """Template for WAITLIST_SPOT_AVAILABLE notification."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display.

        Supports both the legacy broadcast context (``spots_available``) and
        the per-user offer context introduced in the Advanced Waitlist work
        (no ``spots_available`` — one notification per offer).
        """
        event_name = notification.context.get("event_name", "")
        spots = notification.context.get("spots_available")

        if spots is None or spots == 1:
            return _("Spot available for %(event)s!") % {"event": event_name}
        return ngettext(
            "%d spot available for %s!",
            "%d spots available for %s!",
            spots,
        ) % (spots, event_name)

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        event_name = notification.context.get("event_name", "")
        return _("Spot Available: %(event)s") % {"event": event_name}


# Register template
register_template(NotificationType.WAITLIST_SPOT_AVAILABLE, WaitlistSpotAvailableTemplate())
