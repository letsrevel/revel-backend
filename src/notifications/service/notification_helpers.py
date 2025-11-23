"""Helper functions for sending notifications.

This module contains high-level notification helper functions that can be called
from signal handlers or other parts of the application.
"""

import structlog

from common.models import SiteSettings
from events.models import Event
from notifications.enums import NotificationType
from notifications.service.eligibility import get_eligible_users_for_event_notification
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


def notify_event_opened(event: Event) -> int:
    """Send notifications when an event is opened.

    Args:
        event: Event instance or event ID

    Returns:
        Number of notifications sent
    """
    from django.utils.dateformat import format as date_format

    # Get all eligible users for notification
    eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)

    # Build location string
    event_location = event.full_address()

    # Build frontend URL
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    frontend_url = f"{frontend_base_url}/events/{event.id}"

    # Format dates
    event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T") if event.start else ""
    event_end_formatted = date_format(event.end, "l, F j, Y \\a\\t g:i A T") if event.end else ""

    # Format registration opens date if available
    registration_opens_at = None
    if hasattr(event, "registration_opens_at") and event.registration_opens_at:
        registration_opens_at = date_format(event.registration_opens_at, "l, F j, Y \\a\\t g:i A T")

    count = 0
    for user in eligible_users:
        context = {
            "event_id": str(event.id),
            "event_name": event.name,
            "event_description": event.description or "",
            "event_description_html": event.description_html or "",  # type: ignore[attr-defined]
            "event_start": event.start.isoformat() if event.start else "",
            "event_start_formatted": event_start_formatted,
            "event_end": event.end.isoformat() if event.end else "",
            "event_location": event_location,
            "event_url": frontend_url,
            "organization_id": str(event.organization.id),
            "organization_name": event.organization.name,
            "rsvp_required": not event.requires_ticket,
            "tickets_available": event.requires_ticket,
            "questionnaire_required": event.org_questionnaires.exists(),
        }

        if event_end_formatted:
            context["event_end_formatted"] = event_end_formatted
        if registration_opens_at:
            context["registration_opens_at"] = registration_opens_at

        notification_requested.send(
            sender=notify_event_opened,
            user=user,
            notification_type=NotificationType.EVENT_OPEN,
            context=context,
        )
        count += 1

    logger.info(
        "event_open_notifications_sent",
        event_id=str(event.id),
        count=count,
    )

    return count
