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

    IMPORTANT: This handler MUST NOT raise exceptions to prevent crashes in endpoints/signals.
    All errors are logged extensively and swallowed.

    Expected kwargs:
        - notification_type: NotificationType enum value or string
        - user: RevelUser instance
        - context: dict matching the notification type's context schema

    Args:
        sender: Signal sender (usually the class that sent the signal)
        **kwargs: Signal arguments
    """
    try:
        notification_type = kwargs.get("notification_type")
        user = kwargs.get("user")
        context = kwargs.get("context", {})

        if not notification_type or not user:
            logger.error(
                "invalid_notification_request",
                notification_type=notification_type,
                user=user,
                sender=sender,
                kwargs=kwargs,
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
    except Exception as e:
        # CRITICAL: Never let notification errors crash endpoints/signals
        user = kwargs.get("user")
        logger.exception(
            "notification_request_failed",
            notification_type=kwargs.get("notification_type"),
            user_id=str(user.id) if user else None,
            sender=sender.__name__ if hasattr(sender, "__name__") else str(sender),
            context=kwargs.get("context", {}),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
