"""Helper functions for sending notifications.

This module contains high-level notification helper functions that can be called
from signal handlers or other parts of the application.
"""

import typing as t
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from django.utils import timezone
from django.utils.dateformat import format as date_format

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event
from notifications.enums import NotificationType
from notifications.service.dispatcher import NotificationData, bulk_create_notifications
from notifications.service.eligibility import (
    BatchParticipationChecker,
    get_eligible_users_for_event_notification,
)

logger = structlog.get_logger(__name__)

# Default date format for notifications: "Friday, February 6, 2026 at 7:00 PM CET"
DEFAULT_DATE_FORMAT = "l, F j, Y \\a\\t g:i A T"


def get_event_timezone(event: Event) -> ZoneInfo:
    """Get the timezone for an event based on its city.

    Falls back to UTC if no city or timezone is set.

    Args:
        event: Event instance

    Returns:
        ZoneInfo for the event's timezone
    """
    if event.city and event.city.timezone:
        try:
            return ZoneInfo(event.city.timezone)
        except KeyError:
            logger.warning(
                "invalid_timezone_for_city",
                city_id=event.city.id,
                timezone=event.city.timezone,
            )
    return ZoneInfo("UTC")


def format_event_datetime(
    dt: datetime | None,
    event: Event,
    fmt: str = DEFAULT_DATE_FORMAT,
) -> str:
    r"""Format a datetime in the event's timezone.

    Args:
        dt: Datetime to format (must be timezone-aware)
        event: Event to get timezone from
        fmt: Date format string (default: "l, F j, Y \a\t g:i A T")

    Returns:
        Formatted datetime string, or empty string if dt is None
    """
    if not dt:
        return ""

    event_tz = get_event_timezone(event)
    # Convert the datetime to the event's timezone
    dt_in_event_tz = dt.astimezone(event_tz)
    # Use timezone.override to ensure Django's date_format uses the correct timezone
    with timezone.override(event_tz):
        return date_format(dt_in_event_tz, fmt)


def _get_event_location_for_user(event: Event, user: RevelUser) -> tuple[str, str]:
    """Get event location info respecting address visibility for the user.

    Args:
        event: Event to get location for.
        user: User to check visibility for.

    Returns:
        Tuple of (event_location, address_url). Both may be empty strings
        if user cannot see the address.
    """
    if event.can_user_see_address(user):
        return event.full_address(), event.location_maps_url or ""
    return "", ""


def notify_event_opened(event: Event) -> int:
    """Send notifications when an event is opened.

    Uses bulk notification creation for efficiency:
    - Single bulk INSERT for all notifications
    - Single batch dispatch task

    Args:
        event: Event instance or event ID

    Returns:
        Number of notifications sent
    """
    from notifications.tasks import dispatch_notifications_batch

    # Get all eligible users for notification
    eligible_users = list(get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN))

    if not eligible_users:
        logger.info(
            "event_open_notifications_sent",
            event_id=str(event.id),
            count=0,
        )
        return 0

    # Build frontend URL
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    frontend_url = f"{frontend_base_url}/events/{event.id}"

    # Format dates in event's timezone
    event_start_formatted = format_event_datetime(event.start, event)
    event_end_formatted = format_event_datetime(event.end, event)

    # Format registration opens date if available
    registration_opens_at = None
    if hasattr(event, "registration_opens_at") and event.registration_opens_at:
        registration_opens_at = format_event_datetime(event.registration_opens_at, event)

    # Pre-compute event-level data outside the loop to avoid N+1 queries
    questionnaire_required = event.org_questionnaires.exists()

    # Create batch checker for O(1) address visibility lookups
    batch_checker = BatchParticipationChecker(event)

    # Pre-compute full address (only computed if any user can see it)
    full_address = event.full_address()
    maps_url = event.location_maps_url or ""

    # Build list of notifications to create
    notifications_data: list[NotificationData] = []

    for user in eligible_users:
        # Check address visibility per user (O(1) set lookup via batch checker)
        if user.is_superuser or user.is_staff or batch_checker.can_see_address(user.id):
            event_location = full_address
            address_url = maps_url
        else:
            event_location = ""
            address_url = ""

        context: dict[str, t.Any] = {
            "event_id": str(event.id),
            "event_name": event.name,
            "event_description": event.description or "",
            "event_start": event.start.isoformat() if event.start else "",
            "event_start_formatted": event_start_formatted,
            "event_end": event.end.isoformat() if event.end else "",
            "event_location": event_location,
            "event_url": frontend_url,
            "organization_id": str(event.organization.id),
            "organization_name": event.organization.name,
            "rsvp_required": not event.requires_ticket,
            "tickets_available": event.requires_ticket,
            "questionnaire_required": questionnaire_required,
        }

        if event_end_formatted:
            context["event_end_formatted"] = event_end_formatted
        if registration_opens_at:
            context["registration_opens_at"] = registration_opens_at
        if address_url:
            context["address_url"] = address_url

        notifications_data.append(
            NotificationData(
                notification_type=NotificationType.EVENT_OPEN,
                user=user,
                context=context,
            )
        )

    # Bulk create all notifications (single INSERT)
    created_notifications = bulk_create_notifications(notifications_data)

    # Dispatch all notifications in a batch task
    notification_ids = [str(n.id) for n in created_notifications]
    dispatch_notifications_batch.delay(notification_ids)

    logger.info(
        "event_open_notifications_sent",
        event_id=str(event.id),
        count=len(created_notifications),
    )

    return len(created_notifications)
