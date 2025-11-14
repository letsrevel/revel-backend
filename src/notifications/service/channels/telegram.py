"""Telegram notification channel implementation."""

import structlog
from django.utils import timezone

from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.base import NotificationChannel

logger = structlog.get_logger(__name__)


def update_delivery_status(delivery_id: str, status: str, error_message: str | None = None) -> None:
    """Callback function to update delivery status after Telegram sends message.

    This function is called by the telegram task after it completes (success or failure).

    Args:
        delivery_id: UUID of NotificationDelivery to update
        status: New status ("SENT" or "FAILED")
        error_message: Optional error message if failed
    """
    try:
        delivery = NotificationDelivery.objects.get(pk=delivery_id)

        delivery.status = DeliveryStatus(status)
        if status == DeliveryStatus.SENT:
            delivery.delivered_at = timezone.now()
        elif error_message:
            delivery.error_message = error_message

        delivery.save(update_fields=["status", "delivered_at", "error_message", "updated_at"])

        logger.info(
            "telegram_delivery_status_updated",
            delivery_id=delivery_id,
            status=status,
            error=error_message,
        )
    except NotificationDelivery.DoesNotExist:
        logger.error("delivery_not_found_for_callback", delivery_id=delivery_id)
    except Exception as e:
        logger.exception("delivery_status_update_failed", delivery_id=delivery_id, error=str(e), exc_info=True)


class TelegramChannel(NotificationChannel):
    """Telegram notification channel."""

    def get_channel_name(self) -> str:
        """Return channel name."""
        return DeliveryChannel.TELEGRAM

    def can_deliver(self, notification: Notification) -> bool:
        """Check if telegram message can be sent to user.

        Args:
            notification: The notification to check

        Returns:
            True if telegram message can be delivered
        """
        prefs = notification.user.notification_preferences

        # Check if telegram channel is enabled
        if not prefs.is_channel_enabled(DeliveryChannel.TELEGRAM):
            logger.debug(
                "telegram_channel_disabled",
                notification_id=str(notification.id),
                user_id=str(notification.user.id),
            )
            return False

        # Check if notification type is enabled
        if not prefs.is_notification_type_enabled(notification.notification_type):
            logger.debug(
                "notification_type_disabled",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
            )
            return False

        # Check if user has telegram connected (get first active telegram account)
        tg_user = notification.user.telegram_users.filter(blocked_by_user=False, user_is_deactivated=False).first()

        if not tg_user:
            logger.debug(
                "telegram_not_connected",
                notification_id=str(notification.id),
                user_id=str(notification.user.id),
            )
            return False

        if not tg_user.telegram_id:
            logger.debug(
                "telegram_id_missing",
                notification_id=str(notification.id),
                user_id=str(notification.user.id),
            )
            return False

        return True

    def deliver(self, notification: Notification, delivery: NotificationDelivery) -> bool:
        """Send telegram notification with callback for status updates.

        Args:
            notification: The notification to deliver
            delivery: The delivery record to update

        Returns:
            True if delivery was dispatched successfully
        """
        delivery.attempted_at = timezone.now()
        delivery.retry_count += 1

        try:
            # Get first active telegram account
            tg_user = notification.user.telegram_users.filter(blocked_by_user=False, user_is_deactivated=False).first()

            if not tg_user:
                raise ValueError("No active telegram account found for user")

            # Format message for Telegram
            message = self._format_telegram_message(notification)

            # Prepare callback data for status update
            callback_data = {
                "module": "notifications.service.channels.telegram",
                "function": "update_delivery_status",
                "kwargs": {
                    "delivery_id": str(delivery.id),
                },
            }

            # Send via telegram task with callback
            from telegram.tasks import send_message_task

            result = send_message_task.delay(
                tg_user.telegram_id,
                message=message,
                callback_data=callback_data,
            )

            # Mark as PENDING (will be updated by callback)
            delivery.metadata["telegram_task_id"] = result.id
            delivery.save(
                update_fields=[
                    "metadata",
                ]
            )

            logger.info(
                "telegram_notification_dispatched",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
                task_id=result.id,
                callback_data=callback_data,
            )

            return True

        except Exception as e:
            delivery.status = DeliveryStatus.FAILED
            delivery.error_message = str(e)
            delivery.save(update_fields=["status", "error_message", "retry_count", "attempted_at", "updated_at"])

            logger.error(
                "telegram_notification_dispatch_failed",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
                error=str(e),
            )

            return False

    def _format_telegram_message(self, notification: Notification) -> str:
        """Format notification for Telegram (plain text with markdown).

        Args:
            notification: The notification to format

        Returns:
            Formatted message for Telegram
        """
        # TODO: Use template registry for telegram-specific formatting
        # For now, simple formatting
        return f"**{notification.title}**\n\n{notification.body}"
