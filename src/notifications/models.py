"""Models for the notification system."""

from datetime import time

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import RevelUser
from common.fields import MarkdownField
from common.models import TimeStampedModel
from notifications.enums import DeliveryChannel, DeliveryStatus, NotificationType


class Notification(TimeStampedModel):
    """Core notification record - channel agnostic.

    All contextual information (event, organization, ticket, etc.) is stored
    in the structured context JSON field. Only user FK is kept for efficient
    querying of user's notifications.
    """

    # Type and content
    notification_type = models.CharField(
        max_length=50,
        db_index=True,
        choices=NotificationType.choices,
        help_text="Type of notification (StrEnum value)",
    )

    title = models.CharField(max_length=255, blank=True, default="", help_text="Rendered notification title")

    body = MarkdownField(blank=True, default="", help_text="Rendered notification body (markdown/HTML)")

    # Recipient - ONLY FK allowed
    user = models.ForeignKey(RevelUser, on_delete=models.CASCADE, related_name="notifications", db_index=True)

    # Structured context for rendering
    context = models.JSONField(default=dict, help_text="Structured context data (validated TypedDict)")

    # Attachments metadata (for email channel)
    attachments = models.JSONField(
        default=dict, blank=True, help_text="Attachment metadata: {filename: {content_base64: str, mimetype: str}}"
    )

    # In-app notification state
    read_at = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="When user marked notification as read"
    )

    archived_at = models.DateTimeField(null=True, blank=True, help_text="When user archived notification")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "notification_type", "created_at"]),
            models.Index(fields=["user", "read_at"]),  # Unread notifications query
            models.Index(fields=["user", "created_at"]),  # User's notifications timeline
            models.Index(fields=["created_at"]),  # Cleanup task
        ]
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"

    def __str__(self) -> str:
        return f"{self.notification_type} for user {self.user_id} at {self.created_at}"

    def mark_read(self) -> None:
        """Mark notification as read."""
        if not self.read_at:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at"])

    def mark_unread(self) -> None:
        """Mark notification as unread."""
        if self.read_at:
            self.read_at = None
            self.save(update_fields=["read_at"])

    @property
    def is_read(self) -> bool:
        """Check if notification has been read."""
        return self.read_at is not None


class NotificationDelivery(TimeStampedModel):
    """Tracks delivery attempts for each channel.

    One Notification can have multiple NotificationDelivery records
    (one per enabled channel).
    """

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="deliveries")

    channel = models.CharField(
        max_length=20,
        choices=DeliveryChannel.choices,
        db_index=True,
    )

    status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
        db_index=True,
    )

    # Delivery tracking
    attempted_at = models.DateTimeField(null=True, blank=True, help_text="When delivery was first attempted")

    delivered_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="When delivery succeeded")

    # Error tracking
    error_message = models.TextField(blank=True, help_text="Error message if delivery failed")

    retry_count = models.PositiveIntegerField(default=0, help_text="Number of retry attempts")

    # Channel-specific metadata
    metadata = models.JSONField(
        default=dict, blank=True, help_text="Channel-specific data (email_log_id, telegram_msg_id, etc.)"
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["notification", "channel"], name="unique_notification_channel")]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["channel", "status"]),
            models.Index(fields=["notification", "channel"]),
        ]
        verbose_name = "Notification Delivery"
        verbose_name_plural = "Notification Deliveries"

    def __str__(self) -> str:
        return f"{self.notification.notification_type} via {self.channel} - {self.status}"


class NotificationPreference(TimeStampedModel):
    """User's notification preferences.

    Centralizes all notification-related preferences that were previously
    scattered across multiple models in the events app.
    """

    class DigestFrequency(models.TextChoices):
        IMMEDIATE = "immediate", _("Immediate")
        HOURLY = "hourly", _("Hourly digest")
        DAILY = "daily", _("Daily digest")
        WEEKLY = "weekly", _("Weekly digest")

    class VisibilityPreference(models.TextChoices):
        ALWAYS = "always", _("Always display")
        NEVER = "never", _("Never display")
        TO_MEMBERS = "to_members", _("Visible to other organization members")
        TO_INVITEES = "to_invitees", _("Visible to other invitees at the same event")
        TO_BOTH = "to_both", _("Visible to both")

    user = models.OneToOneField(RevelUser, on_delete=models.CASCADE, related_name="notification_preferences")

    # Global notification settings
    silence_all_notifications = models.BooleanField(
        default=False, help_text="Master switch to disable all notifications"
    )

    enabled_channels = ArrayField(
        models.CharField(max_length=20),
        default=list,
        blank=True,
        help_text="List of enabled channels: ['in_app', 'email', 'telegram']",
    )

    # Digest preferences
    digest_frequency = models.CharField(
        max_length=20,
        choices=DigestFrequency.choices,
        default=DigestFrequency.IMMEDIATE,
        help_text="How often to batch notifications",
    )

    digest_send_time = models.TimeField(
        default=time(9, 0), help_text="Preferred time for daily/weekly digests (user's local time)"
    )

    # Per-notification-type preferences (JSON for flexibility)
    notification_type_settings = models.JSONField(
        default=dict, blank=True, help_text="Per-type settings: {notification_type: {enabled: bool, channels: []}}"
    )

    # Event reminders
    event_reminders_enabled = models.BooleanField(
        default=True, help_text="Receive reminders 14, 7, 1 days before events"
    )

    # Visibility (from old preferences)
    show_me_on_attendee_list = models.CharField(
        choices=VisibilityPreference.choices,
        max_length=20,
        default=VisibilityPreference.NEVER,
        help_text="Attendee list visibility preference",
    )

    # Default city (from old GeneralUserPreferences)
    city = models.ForeignKey("geo.City", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "Notification Preference"
        verbose_name_plural = "Notification Preferences"

    def __str__(self) -> str:
        return f"{self.user_id} notification preferences"

    def is_channel_enabled(self, channel: str) -> bool:
        """Check if a channel is enabled for this user.

        Args:
            channel: Channel name (e.g., 'email', 'in_app', 'telegram')

        Returns:
            True if channel is enabled
        """
        if self.silence_all_notifications:
            return False
        return bool(channel in self.enabled_channels)

    def is_notification_type_enabled(self, notification_type: str) -> bool:
        """Check if a specific notification type is enabled.

        Args:
            notification_type: Notification type (e.g., 'ticket_created')

        Returns:
            True if notification type is enabled
        """
        if self.silence_all_notifications:
            return False

        settings = self.notification_type_settings.get(notification_type, {})
        return bool(settings.get("enabled", True))  # Default to enabled

    def get_channels_for_notification_type(self, notification_type: str) -> list[str]:
        """Get enabled channels for a specific notification type.

        Args:
            notification_type: Notification type

        Returns:
            List of enabled channel names
        """
        if self.silence_all_notifications:
            return []

        # Check if notification type is enabled
        if not self.is_notification_type_enabled(notification_type):
            return []

        # Check if notification type has custom channel settings
        settings = self.notification_type_settings.get(notification_type, {})
        custom_channels = settings.get("channels", [])

        if custom_channels:
            # Use custom channels if specified
            return [ch for ch in custom_channels if ch in self.enabled_channels]

        # Otherwise use all enabled channels
        return list(self.enabled_channels)
