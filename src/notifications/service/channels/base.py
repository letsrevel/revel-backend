"""Base channel interface for notification delivery."""

from abc import ABC, abstractmethod

from notifications.models import Notification, NotificationDelivery


class NotificationChannel(ABC):
    """Abstract base class for notification delivery channels."""

    @abstractmethod
    def get_channel_name(self) -> str:
        """Return the channel identifier (matches DeliveryChannel enum).

        Returns:
            Channel name (e.g., 'in_app', 'email', 'telegram')
        """
        pass

    @abstractmethod
    def can_deliver(self, notification: Notification) -> bool:
        """Check if this channel can deliver the notification.

        Considers user preferences and channel-specific requirements.

        Args:
            notification: The notification to check

        Returns:
            True if notification can be delivered through this channel
        """
        pass

    @abstractmethod
    def deliver(self, notification: Notification, delivery: NotificationDelivery) -> bool:
        """Deliver notification through this channel.

        Updates delivery record with status, timestamps, and any errors.

        Args:
            notification: The notification to deliver
            delivery: The delivery record to update

        Returns:
            True if delivery succeeded, False otherwise
        """
        pass

    def should_retry(self, error: Exception) -> bool:
        """Determine if delivery should be retried based on error type.

        Override in subclasses for channel-specific retry logic.

        Args:
            error: The exception that was raised

        Returns:
            True if delivery should be retried
        """
        from smtplib import SMTPException

        # Common retryable errors
        retryable = (SMTPException, OSError, TimeoutError, ConnectionError)
        return isinstance(error, retryable)
