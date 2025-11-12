"""In-app notification channel implementation."""

import structlog
from django.utils import timezone

from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.base import NotificationChannel

logger = structlog.get_logger(__name__)


class InAppChannel(NotificationChannel):
    """In-app notification channel.

    This is a no-op channel since the notification record itself
    serves as the in-app notification. We just mark delivery as sent.
    """

    def get_channel_name(self) -> str:
        """Return channel name."""
        return DeliveryChannel.IN_APP

    def can_deliver(self, notification: Notification) -> bool:
        """In-app notifications are always deliverable (already in DB).

        Args:
            notification: The notification to check

        Returns:
            True if user has in-app channel enabled
        """
        return True

    def deliver(self, notification: Notification, delivery: NotificationDelivery) -> bool:
        """Mark in-app delivery as sent (notification already exists).

        Args:
            notification: The notification to deliver
            delivery: The delivery record to update

        Returns:
            True if successful
        """
        delivery.status = DeliveryStatus.SENT
        delivery.attempted_at = timezone.now()
        delivery.delivered_at = timezone.now()
        delivery.save(update_fields=["status", "attempted_at", "delivered_at", "updated_at"])

        logger.info(
            "in_app_notification_delivered",
            notification_id=str(notification.id),
            notification_type=notification.notification_type,
            user_id=str(notification.user.id),
        )

        return True
