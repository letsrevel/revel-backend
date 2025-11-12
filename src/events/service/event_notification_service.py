"""Service for event-related notifications."""

from uuid import UUID

import structlog

from events.models import Event
from events.service.notification_service import get_eligible_users_for_event_notification
from notifications.enums import NotificationType
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


def notify_event_opened(event: Event | UUID) -> int:
    """Send notifications when an event is opened.

    Args:
        event: Event instance or event ID

    Returns:
        Number of notifications sent
    """
    if isinstance(event, UUID):
        event = Event.objects.select_related("organization").get(pk=event)

    # Get all eligible users for notification
    eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)

    count = 0
    for user in eligible_users:
        notification_requested.send(
            sender=notify_event_opened,
            user=user,
            notification_type=NotificationType.EVENT_OPEN,
            context={
                "event_id": str(event.id),
                "event_name": event.name,
                "event_start": event.start.isoformat() if event.start else "",
                "event_description": event.description or "",
                "organization_id": str(event.organization.id),
                "organization_name": event.organization.name,
            },
        )
        count += 1

    logger.info(
        "event_open_notifications_sent",
        event_id=str(event.id),
        count=count,
    )

    return count
