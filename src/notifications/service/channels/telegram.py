"""Telegram notification channel implementation."""

import structlog
from django.utils import timezone

from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.base import NotificationChannel

logger = structlog.get_logger(__name__)


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

        # Check if user has telegram connected
        if not hasattr(notification.user, "telegram_user"):
            logger.debug(
                "telegram_not_connected",
                notification_id=str(notification.id),
                user_id=str(notification.user.id),
            )
            return False

        if not notification.user.telegram_user.telegram_id:
            logger.debug(
                "telegram_id_missing",
                notification_id=str(notification.id),
                user_id=str(notification.user.id),
            )
            return False

        return True

    def deliver(self, notification: Notification, delivery: NotificationDelivery) -> bool:
        """Send telegram notification.

        Args:
            notification: The notification to deliver
            delivery: The delivery record to update

        Returns:
            True if delivery succeeded
        """
        delivery.attempted_at = timezone.now()
        delivery.retry_count += 1

        try:
            # Format message for Telegram
            message = self._format_telegram_message(notification)

            # Send via existing telegram task
            from telegram.tasks import send_message_task

            result = send_message_task.delay(notification.user.telegram_user.telegram_id, message=message)

            # Update delivery record
            delivery.status = DeliveryStatus.SENT
            delivery.delivered_at = timezone.now()
            delivery.metadata["telegram_task_id"] = result.id
            delivery.save(
                update_fields=[
                    "status",
                    "delivered_at",
                    "metadata",
                    "retry_count",
                    "attempted_at",
                    "updated_at",
                ]
            )

            logger.info(
                "telegram_notification_sent",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
            )

            return True

        except Exception as e:
            delivery.status = DeliveryStatus.FAILED
            delivery.error_message = str(e)
            delivery.save(update_fields=["status", "error_message", "retry_count", "attempted_at", "updated_at"])

            logger.error(
                "telegram_notification_failed",
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
