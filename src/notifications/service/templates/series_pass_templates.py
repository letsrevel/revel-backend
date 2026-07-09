"""Templates for series pass notifications."""

from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class SeriesPassPurchasedTemplate(NotificationTemplate):
    """Template for SERIES_PASS_PURCHASED notification (to holder + org staff/owners)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        pass_name = notification.context.get("pass_name", "your pass")
        series_name = notification.context.get("series_name", "the series")
        return _("Series pass purchased: %(pass)s (%(series)s)") % {
            "pass": pass_name,
            "series": series_name,
        }

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        pass_name = notification.context.get("pass_name", "your pass")
        return _("Series pass purchased - %(pass)s") % {"pass": pass_name}


class SeriesPassExtendedTemplate(NotificationTemplate):
    """Template for SERIES_PASS_EXTENDED notification (to the pass holder)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        pass_name = notification.context.get("pass_name", "your pass")
        count = notification.context.get("new_event_count", 0)
        return ngettext(
            "%(pass)s now covers %(count)d new event",
            "%(pass)s now covers %(count)d new events",
            count,
        ) % {"pass": pass_name, "count": count}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        pass_name = notification.context.get("pass_name", "your pass")
        return _("Your series pass has new events - %(pass)s") % {"pass": pass_name}


class SeriesPassCancelledTemplate(NotificationTemplate):
    """Template for SERIES_PASS_CANCELLED notification (to holder + org staff/owners)."""

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        pass_name = notification.context.get("pass_name", "your pass")
        series_name = notification.context.get("series_name", "the series")
        return _("Series pass cancelled: %(pass)s (%(series)s)") % {
            "pass": pass_name,
            "series": series_name,
        }

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        pass_name = notification.context.get("pass_name", "your pass")
        return _("Series pass cancelled - %(pass)s") % {"pass": pass_name}


# Register templates
register_template(NotificationType.SERIES_PASS_PURCHASED, SeriesPassPurchasedTemplate())
register_template(NotificationType.SERIES_PASS_EXTENDED, SeriesPassExtendedTemplate())
register_template(NotificationType.SERIES_PASS_CANCELLED, SeriesPassCancelledTemplate())
