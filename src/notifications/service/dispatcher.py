"""Core notification dispatcher service."""

import typing as t
from collections.abc import Sequence

import structlog

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import Notification, NotificationPreference

logger = structlog.get_logger(__name__)


class NotificationData(t.NamedTuple):
    """Data for creating a notification."""

    notification_type: NotificationType
    user: RevelUser
    context: dict[str, t.Any]


def create_notification(
    notification_type: NotificationType | str,
    user: RevelUser,
    context: dict[str, t.Any],
) -> Notification:
    """Create a notification record.

    Args:
        notification_type: Type of notification
        user: User to notify
        context: Notification context data

    Returns:
        Created Notification instance

    Raises:
        ValueError: If context validation fails
    """
    from notifications.context_schemas import validate_notification_context

    # Convert string to enum if needed
    if isinstance(notification_type, str):
        notification_type = NotificationType(notification_type)

    # Validate context
    validate_notification_context(notification_type, context)

    # Create notification (title/body will be rendered by dispatcher task)
    notification = Notification.objects.create(
        notification_type=notification_type,
        user=user,
        context=context,
        title="",  # Will be rendered by dispatcher
        body="",  # Will be rendered by dispatcher
    )

    logger.info(
        "notification_created",
        notification_id=str(notification.id),
        notification_type=notification_type,
        user_id=str(user.id),
    )

    return notification


def bulk_create_notifications(
    notifications_data: Sequence[NotificationData],
) -> list[Notification]:
    """Create multiple notification records in a single database operation.

    This is more efficient than calling create_notification() in a loop because:
    1. Single bulk INSERT instead of N individual INSERTs
    2. Validates all contexts upfront before any database operations

    Args:
        notifications_data: Sequence of NotificationData tuples

    Returns:
        List of created Notification instances with IDs populated

    Raises:
        ValueError: If any context validation fails (no notifications created)
    """
    from notifications.context_schemas import validate_notification_context

    if not notifications_data:
        return []

    # Validate all contexts first (fail fast before any DB operations)
    for data in notifications_data:
        notification_type = data.notification_type
        if isinstance(notification_type, str):
            notification_type = NotificationType(notification_type)
        validate_notification_context(notification_type, data.context)

    # Build notification objects
    notifications_to_create = [
        Notification(
            notification_type=data.notification_type,
            user=data.user,
            context=data.context,
            title="",  # Will be rendered by dispatcher task
            body="",  # Will be rendered by dispatcher task
        )
        for data in notifications_data
    ]

    # Bulk create (single INSERT)
    created_notifications = Notification.objects.bulk_create(notifications_to_create)

    logger.info(
        "notifications_bulk_created",
        count=len(created_notifications),
        notification_type=notifications_data[0].notification_type if notifications_data else None,
    )

    return created_notifications


def determine_delivery_channels(user: RevelUser, notification_type: str) -> list[str]:
    """Determine which channels should receive this notification.

    Args:
        user: User to notify
        notification_type: Type of notification

    Returns:
        List of channel names to deliver to
    """
    prefs = user.notification_preferences

    # Check if user wants digest
    if prefs.digest_frequency != NotificationPreference.DigestFrequency.IMMEDIATE:
        # Only create in-app notification, email will be sent in digest
        return [DeliveryChannel.IN_APP]

    # Get enabled channels for this notification type
    channels = prefs.get_channels_for_notification_type(notification_type)

    logger.debug(
        "determined_delivery_channels",
        user_id=str(user.id),
        notification_type=notification_type,
        channels=channels,
    )

    return channels
