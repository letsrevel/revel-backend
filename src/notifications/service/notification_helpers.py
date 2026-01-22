"""Helper functions for sending notifications.

This module contains high-level notification helper functions that can be called
from signal handlers or other parts of the application.
"""

import typing as t

import structlog

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


def _get_event_location_for_user_batch(
    event: Event,
    user: RevelUser,
    batch_checker: BatchParticipationChecker,
) -> tuple[str, str]:
    """Get event location info respecting address visibility (batch-optimized).

    Uses BatchParticipationChecker for O(1) visibility lookup instead of per-user queries.

    Args:
        event: Event to get location for.
        user: User to check visibility for.
        batch_checker: Pre-populated batch checker for O(1) lookups.

    Returns:
        Tuple of (event_location, address_url). Both may be empty strings
        if user cannot see the address.
    """
    # Check superuser/staff first (batch checker doesn't handle these)
    if user.is_superuser or user.is_staff:
        return event.full_address(), event.location_maps_url or ""

    if batch_checker.can_see_address(user.id):
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
    from django.utils.dateformat import format as date_format

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

    # Format dates
    event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T") if event.start else ""
    event_end_formatted = date_format(event.end, "l, F j, Y \\a\\t g:i A T") if event.end else ""

    # Format registration opens date if available
    registration_opens_at = None
    if hasattr(event, "registration_opens_at") and event.registration_opens_at:
        registration_opens_at = date_format(event.registration_opens_at, "l, F j, Y \\a\\t g:i A T")

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
