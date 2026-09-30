"""Models for the notification system."""

import typing as t
from datetime import time

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import RevelUser
from common.fields import MarkdownField
from common.models import TimeStampedModel
from notifications.enums import MANDATORY_TYPES, DeliveryStatus, NotificationType

from .enums import DeliveryChannel
from .types import NotificationTypeSetting


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
            # Composite index for event reminder deduplication
            models.Index(
                fields=["notification_type", "user"],
                name="notif_type_user_idx",
            ),
            # GIN index for JSONB context queries (e.g., context->'event_id')
            GinIndex(
                fields=["context"],
                name="notif_context_gin_idx",
                opclasses=["jsonb_path_ops"],
            ),
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


def get_default_notification_type_settings() -> dict[NotificationType, NotificationTypeSetting]:
    """Get the default notification type settings.

    Certain notification types default to IN_APP only to avoid spamming users:
    - Potluck notifications
    - Follow notifications (when someone follows an org/series)
    """
    return {
        # Potluck notifications - IN_APP only
        NotificationType.POTLUCK_ITEM_CLAIMED: NotificationTypeSetting(enabled=True, channels=[DeliveryChannel.IN_APP]),
        NotificationType.POTLUCK_ITEM_CREATED: NotificationTypeSetting(enabled=True, channels=[DeliveryChannel.IN_APP]),
        NotificationType.POTLUCK_ITEM_CREATED_AND_CLAIMED: NotificationTypeSetting(
            enabled=True, channels=[DeliveryChannel.IN_APP]
        ),
        NotificationType.POTLUCK_ITEM_UPDATED: NotificationTypeSetting(enabled=True, channels=[DeliveryChannel.IN_APP]),
        NotificationType.POTLUCK_ITEM_UNCLAIMED: NotificationTypeSetting(
            enabled=True, channels=[DeliveryChannel.IN_APP]
        ),
        NotificationType.POTLUCK_ITEM_DELETED: NotificationTypeSetting(enabled=True, channels=[DeliveryChannel.IN_APP]),
        # Follow notifications - IN_APP only (to org admins when someone follows)
        NotificationType.ORGANIZATION_FOLLOWED: NotificationTypeSetting(
            enabled=True, channels=[DeliveryChannel.IN_APP]
        ),
        NotificationType.EVENT_SERIES_FOLLOWED: NotificationTypeSetting(
            enabled=True, channels=[DeliveryChannel.IN_APP]
        ),
        # Org contact form messages - IN_APP + TELEGRAM (the transactional email is
        # sent separately to the org's mailbox via the events email task; the dispatcher
        # email channel would fan out to every staff user, which is not desired).
        NotificationType.ORG_CONTACT_MESSAGE_RECEIVED: NotificationTypeSetting(
            enabled=True, channels=[DeliveryChannel.IN_APP, DeliveryChannel.TELEGRAM]
        ),
    }


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
        default=False,
        help_text="Master kill switch - disables all notifications including in-app, except mandatory types "
        "(MANDATORY_TYPES)",
    )

    enabled_channels = ArrayField(
        models.CharField(max_length=20),
        default=list,
        blank=True,
        help_text="Default channels for notifications. Can be overridden per notification type.",
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
        default=get_default_notification_type_settings,
        blank=True,
        help_text="Per-type overrides: {notification_type: {enabled: bool, channels: []}}. "
        "Channels specified here OVERRIDE enabled_channels for that type.",
    )

    # Event reminders
    event_reminders_enabled = models.BooleanField(
        default=True, help_text="Receive reminders 14, 7, 1 days before events"
    )

    # Per-organization announcement mute (#1031). Governs ORG_ANNOUNCEMENT only, on all channels.
    muted_organizations = models.ManyToManyField(
        "events.Organization",
        blank=True,
        related_name="+",
        help_text="Organizations whose announcements this user does not want to receive.",
    )

    class Meta:
        verbose_name = "Notification Preference"
        verbose_name_plural = "Notification Preferences"
        indexes = [
            models.Index(fields=["digest_frequency", "digest_send_time"]),
        ]

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

        Uses override semantics: per-type channel settings override global enabled_channels.
        This allows users to say "I generally don't want telegram, BUT for critical alerts send telegram."

        Hierarchy:
            0. MANDATORY_TYPES -> base channels (steps 3/4) plus IN_APP and EMAIL, ignoring
               silence and per-type ``enabled`` (users can't opt out of these)
            1. silence_all_notifications (master kill switch) -> []
            2. notification_type enabled check -> []
            3. notification_type_settings[type].channels (if specified) -> use these channels
            4. Otherwise -> use global enabled_channels

        Args:
            notification_type: Notification type

        Returns:
            List of enabled channel names
        """
        if notification_type in MANDATORY_TYPES:
            channels = self._base_channels(notification_type)
            for required in (DeliveryChannel.IN_APP, DeliveryChannel.EMAIL):
                if required not in channels:
                    channels.append(required)
            return channels

        if self.silence_all_notifications:
            return []

        # Check if notification type is enabled
        if not self.is_notification_type_enabled(notification_type):
            return []

        return self._base_channels(notification_type)

    def _base_channels(self, notification_type: str) -> list[str]:
        """Per-type channel override if set, else the global ``enabled_channels``."""
        settings = self.notification_type_settings.get(notification_type, {})
        custom_channels = settings.get("channels", [])
        if custom_channels:
            # Per-type override - use these channels instead of global settings
            return list(custom_channels)
        return list(self.enabled_channels)

    def disable_email_for_type(self, notification_type: str) -> bool:
        """Stop email for one notification type while keeping its other channels.

        Pins a per-type override of the currently effective channels minus EMAIL. An
        empty ``channels`` list would fall back to ``enabled_channels`` (and email would
        come back), so when nothing else remains the override is pinned to IN_APP.

        Args:
            notification_type: Notification type to stop emailing.

        Returns:
            True if the stored settings changed, False otherwise.
        """
        existing = t.cast(NotificationTypeSetting, self.notification_type_settings.get(notification_type, {}))
        channels = [
            DeliveryChannel(c)
            for c in self.get_channels_for_notification_type(notification_type)
            if c != DeliveryChannel.EMAIL
        ]
        new_setting = NotificationTypeSetting(
            enabled=existing.get("enabled", True),
            channels=channels or [DeliveryChannel.IN_APP],
        )
        if existing == new_setting:
            return False
        self.notification_type_settings[notification_type] = new_setting
        return True

    def enable_channel(self, channel: str) -> bool:
        """Re-enable a delivery channel globally and on its default per-type overrides.

        Mirror of :meth:`disable_channel`: adds ``channel`` to ``enabled_channels`` and
        re-adds it to every per-type override whose default (see
        ``get_default_notification_type_settings``) includes it. Overrides whose default
        doesn't include the channel (e.g. potluck → IN_APP only) are left alone.

        Args:
            channel: Channel to enable (e.g. ``DeliveryChannel.TELEGRAM``).

        Returns:
            True if anything changed, False otherwise.
        """
        changed = False
        if channel not in self.enabled_channels:
            self.enabled_channels = [*self.enabled_channels, channel]
            changed = True
        for notification_type, default in get_default_notification_type_settings().items():
            if channel not in default["channels"]:
                continue
            setting = t.cast(NotificationTypeSetting | None, self.notification_type_settings.get(notification_type))
            # No override (or no channel list) falls back to enabled_channels, fixed above.
            channels = setting.get("channels") if setting is not None else None
            if setting is not None and channels and channel not in channels:
                setting["channels"] = [*channels, DeliveryChannel(channel)]
                changed = True
        return changed

    def disable_channel(self, channel: str) -> bool:
        """Remove a delivery channel from global and per-type preferences.

        Removes ``channel`` from ``enabled_channels`` and from every per-type
        override in ``notification_type_settings``. Per-type channel lists
        OVERRIDE ``enabled_channels`` (see ``get_channels_for_notification_type``),
        so clearing only the global list would leave types that hardcode the
        channel (e.g. ``ORG_CONTACT_MESSAGE_RECEIVED`` → TELEGRAM) still delivering.

        Args:
            channel: Channel to remove (e.g. ``DeliveryChannel.TELEGRAM``).

        Returns:
            True if anything changed, False otherwise.
        """
        changed = False
        if channel in self.enabled_channels:
            self.enabled_channels = [c for c in self.enabled_channels if c != channel]
            changed = True
        for setting in self.notification_type_settings.values():
            channels = setting.get("channels")
            if channels and channel in channels:
                setting["channels"] = [c for c in channels if c != channel]
                changed = True
        return changed


class EmailSuppression(TimeStampedModel):
    """An address Revel must not email (bounce, complaint, block, invalid, or opt-out).

    One row per normalized address; see ``notifications.service.email_policy`` for the
    rank-aware upsert and the lookup rules.

    # ponytail: one row per address → per-org complaint counts = distinct complainers
    # attributed to their strongest/latest; add an event table if triage needs exact counts.
    """

    class Reason(models.TextChoices):
        HARD_BOUNCE = "hard_bounce", _("Hard bounce")
        COMPLAINT = "complaint", _("Spam complaint")
        BLOCKED = "blocked", _("Blocked")
        INVALID = "invalid", _("Invalid address")
        INVITATION_OPT_OUT = "invitation_opt_out", _("Opted out of invitations")

    class Source(models.TextChoices):
        PROVIDER = "provider", _("Email provider")
        RECIPIENT = "recipient", _("Recipient")
        ADMIN = "admin", _("Admin")

    # Higher wins; a write of lower/equal rank never overwrites an existing row.
    REASON_RANK: t.ClassVar[dict[str, int]] = {
        Reason.INVITATION_OPT_OUT: 0,
        Reason.HARD_BOUNCE: 1,
        Reason.INVALID: 1,
        Reason.BLOCKED: 1,
        Reason.COMPLAINT: 2,
    }

    email = models.EmailField(unique=True, help_text="Normalized via normalize_email_for_matching().")
    reason = models.CharField(max_length=32, choices=Reason.choices)
    source = models.CharField(max_length=16, choices=Source.choices)
    organization = models.ForeignKey(
        "events.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Organization whose mail triggered the suppression, if known.",
    )
    detail = models.TextField(blank=True, default="", help_text="Provider reason text.")

    class Meta:
        verbose_name = "Email Suppression"
        verbose_name_plural = "Email Suppressions"

    def __str__(self) -> str:
        return f"{self.email} ({self.reason})"
