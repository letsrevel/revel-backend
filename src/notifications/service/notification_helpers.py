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
    # Get all eligible users for notification
    eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)

    # Build location string
    event_location = event.address or (event.city.name if event.city else "")

    # Build frontend URL
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    frontend_url = f"{frontend_base_url}/events/{event.id}"

    count = 0
    for user in eligible_users:
        notification_requested.send(
            sender=notify_event_opened,
            user=user,
            notification_type=NotificationType.EVENT_OPEN,
            context={
                "event_id": str(event.id),
                "event_name": event.name,
                "event_description": event.description or "",
                "event_description_html": event.description_html or "",  # type: ignore[attr-defined]
                "event_start": event.start.isoformat() if event.start else "",
                "event_end": event.end.isoformat() if event.end else "",
                "event_location": event_location,
                "organization_id": str(event.organization.id),
                "organization_name": event.organization.name,
                "rsvp_required": not event.requires_ticket,
                "tickets_available": event.requires_ticket,
                "questionnaire_required": event.org_questionnaires.exists(),
                "frontend_url": frontend_url,
            },
        )
        count += 1

    logger.info(
        "event_open_notifications_sent",
        event_id=str(event.id),
        count=count,
    )

    return count
