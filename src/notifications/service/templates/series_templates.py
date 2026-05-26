"""Templates for event-series notifications."""

from django.utils.translation import ngettext

from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.base import NotificationTemplate
from notifications.service.templates.registry import register_template


class SeriesEventsGeneratedTemplate(NotificationTemplate):
    """Template for SERIES_EVENTS_GENERATED digest (to series audience).

    Sent when recurring events are materialized for an :class:`EventSeries`.
    The body templates live under ``notifications/{in_app,email,telegram}/
    series_events_generated.*`` and are rendered by the base class; this class
    only supplies the in-app title and email subject.
    """

    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display."""
        series_name = notification.context.get("event_series_name", "a series")
        count = notification.context.get("events_count", 0)
        return ngettext(
            "%(count)d new event scheduled for %(series)s",
            "%(count)d new events scheduled for %(series)s",
            count,
        ) % {"count": count, "series": series_name}

    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject."""
        series_name = notification.context.get("event_series_name", "a series")
        count = notification.context.get("events_count", 0)
        return ngettext(
            "New event scheduled for %(series)s",
            "%(count)d new events scheduled for %(series)s",
            count,
        ) % {"count": count, "series": series_name}


# Register templates
register_template(NotificationType.SERIES_EVENTS_GENERATED, SeriesEventsGeneratedTemplate())
