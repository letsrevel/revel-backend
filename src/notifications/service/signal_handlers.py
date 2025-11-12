"""Signal handlers for notification system."""

import typing as t

import structlog
from django.dispatch import receiver

from notifications.service.dispatcher import create_notification
from notifications.signals import notification_requested

logger = structlog.get_logger(__name__)


@receiver(notification_requested)
def handle_notification_request(sender: t.Any, **kwargs: t.Any) -> None:
    """Handle notification_requested signal.

    Creates notification record and dispatches to async task.

    Expected kwargs:
        - notification_type: NotificationType enum value or string
        - user: RevelUser instance
        - context: dict matching the notification type's context schema

    Args:
        sender: Signal sender (usually the class that sent the signal)
        **kwargs: Signal arguments
    """
    notification_type = kwargs.get("notification_type")
    user = kwargs.get("user")
    context = kwargs.get("context", {})

    if not notification_type or not user:
        logger.error(
            "invalid_notification_request",
            notification_type=notification_type,
            user=user,
            sender=sender,
        )
        return

    # Create notification
    notification = create_notification(
        notification_type=notification_type,
        user=user,
        context=context,
    )

    # Dispatch async
    from notifications.tasks import dispatch_notification

    dispatch_notification.delay(str(notification.id))

    logger.info(
        "notification_request_handled",
        notification_id=str(notification.id),
        notification_type=notification_type,
        user_id=str(user.id),
        sender=sender.__name__ if hasattr(sender, "__name__") else str(sender),
    )
