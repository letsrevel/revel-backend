"""Core notification dispatcher service."""

import typing as t

import structlog

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import Notification, NotificationPreference

logger = structlog.get_logger(__name__)


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
