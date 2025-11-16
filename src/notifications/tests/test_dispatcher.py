"""Tests for notification dispatcher service."""

import pytest

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import NotificationPreference
from notifications.service.dispatcher import create_notification, determine_delivery_channels

pytestmark = pytest.mark.django_db


class TestCreateNotification:
    """Test notification creation."""

    def test_creates_notification_with_empty_title_and_body(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that notifications are created with empty title/body.

        The title and body are rendered asynchronously by the dispatcher task,
        so they start empty and get filled in later.
        """
        # Arrange
        context = {
            "ticket_id": "abc123",
            "ticket_reference": "TKT-001",
            "event_id": "evt123",
            "event_name": "Test Event",
            "event_start": "2025-12-01T18:00:00Z",
            "event_location": "Test Venue",
            "organization_id": "org123",
            "organization_name": "Test Org",
            "tier_name": "General Admission",
            "tier_price": "10.00",
            "quantity": 1,
            "total_price": "10.00",
        }

        # Act
        notification = create_notification(
            notification_type=NotificationType.TICKET_CREATED,
            user=regular_user,
            context=context,
        )

        # Assert
        assert notification.title == ""
        assert notification.body == ""
        assert notification.context == context
        assert notification.notification_type == NotificationType.TICKET_CREATED
        assert notification.user == regular_user

    def test_validates_context_against_schema(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that invalid context raises ValidationError."""
        # Arrange - Missing required fields
        invalid_context = {
            "ticket_id": "abc123",
            "event_id": "evt123",
            # Missing required fields like 'event_name', 'organization_id', etc.
        }

        # Act & Assert
        with pytest.raises(ValueError, match="Missing required context keys"):
            create_notification(
                notification_type=NotificationType.TICKET_CREATED,
                user=regular_user,
                context=invalid_context,
            )

    def test_accepts_string_notification_type(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that notification_type can be passed as string."""
        context = {
            "event_id": "evt123",
            "event_name": "Test Event",
            "event_start": "2025-12-01T18:00:00Z",
            "event_location": "Test Venue",
            "days_until": 7,
        }

        # Act - Pass as string instead of enum
        notification = create_notification(
            notification_type="event_reminder",
            user=regular_user,
            context=context,
        )

        # Assert
        assert notification.notification_type == NotificationType.EVENT_REMINDER


class TestDetermineDeliveryChannels:
    """Test channel determination based on user preferences."""

    def test_returns_all_enabled_channels_for_immediate_mode(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that all enabled channels are returned in immediate mode."""
        # Arrange - User has default preferences (both channels enabled, immediate mode)
        prefs = regular_user.notification_preferences
        assert prefs.digest_frequency == NotificationPreference.DigestFrequency.IMMEDIATE

        # Act
        channels = determine_delivery_channels(regular_user, NotificationType.TICKET_CREATED)

        # Assert
        assert DeliveryChannel.IN_APP in channels
        assert DeliveryChannel.EMAIL in channels

    def test_returns_only_in_app_for_digest_mode(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that only in-app channel is used when user wants digest.

        When digest mode is enabled, notifications are created as in-app only.
        Email delivery happens later via the digest task.
        """
        # Arrange - Set user to daily digest
        prefs = regular_user.notification_preferences
        prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
        prefs.save()

        # Act
        channels = determine_delivery_channels(regular_user, NotificationType.TICKET_CREATED)

        # Assert
        assert channels == [DeliveryChannel.IN_APP]
        assert DeliveryChannel.EMAIL not in channels

    def test_respects_disabled_channels(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that disabled channels are excluded."""
        # Arrange - Disable email channel
        prefs = regular_user.notification_preferences
        prefs.enabled_channels = [DeliveryChannel.IN_APP]
        prefs.save()

        # Act
        channels = determine_delivery_channels(regular_user, NotificationType.TICKET_CREATED)

        # Assert
        assert DeliveryChannel.IN_APP in channels
        assert DeliveryChannel.EMAIL not in channels

    def test_guest_user_restrictions_applied(
        self,
        guest_user: RevelUser,
    ) -> None:
        """Test that guest users can't receive restricted notification types.

        Guest users have POTLUCK notifications disabled, so channel determination
        should return empty list for those types.
        """
        # Act
        channels = determine_delivery_channels(guest_user, NotificationType.POTLUCK_ITEM_CREATED)

        # Assert - Guest users don't get potluck notifications
        assert len(channels) == 0

    def test_guest_user_receives_allowed_notifications(
        self,
        guest_user: RevelUser,
    ) -> None:
        """Test that guest users DO receive allowed notification types."""
        # Act - TICKET_CREATED is allowed for guests
        channels = determine_delivery_channels(guest_user, NotificationType.TICKET_CREATED)

        # Assert
        assert DeliveryChannel.IN_APP in channels
        assert DeliveryChannel.EMAIL in channels

    def test_per_type_channels_override_global_settings(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that per-type channel settings OVERRIDE global enabled_channels.

        This tests the override semantics: if a notification type specifies channels,
        those channels are used INSTEAD OF the global enabled_channels.
        This allows users to say: "I don't want telegram globally, BUT send me
        telegram for critical alerts."
        """
        # Arrange - User has only in_app and email enabled globally
        prefs = regular_user.notification_preferences
        prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL]

        # But for EVENT_REMINDER, override to use only telegram (not in enabled_channels!)
        prefs.notification_type_settings = {
            NotificationType.EVENT_REMINDER: {
                "enabled": True,
                "channels": [DeliveryChannel.TELEGRAM],  # Override!
            }
        }
        prefs.save()

        # Act
        channels = determine_delivery_channels(regular_user, NotificationType.EVENT_REMINDER)

        # Assert - Should use override channels (telegram), NOT global channels
        assert channels == [DeliveryChannel.TELEGRAM]
        assert DeliveryChannel.IN_APP not in channels
        assert DeliveryChannel.EMAIL not in channels

    def test_per_type_channels_can_restrict_global_settings(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that per-type settings can also restrict channels."""
        # Arrange - User has all three channels enabled globally
        prefs = regular_user.notification_preferences
        prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL, DeliveryChannel.TELEGRAM]

        # But for TICKET_CREATED, only use email
        prefs.notification_type_settings = {
            NotificationType.TICKET_CREATED: {
                "enabled": True,
                "channels": [DeliveryChannel.EMAIL],
            }
        }
        prefs.save()

        # Act
        channels = determine_delivery_channels(regular_user, NotificationType.TICKET_CREATED)

        # Assert - Should only include email
        assert channels == [DeliveryChannel.EMAIL]
        assert DeliveryChannel.IN_APP not in channels
        assert DeliveryChannel.TELEGRAM not in channels

    def test_uses_global_channels_when_no_per_type_override(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that global enabled_channels are used when no per-type override exists."""
        # Arrange - User has specific channels enabled, no per-type settings
        prefs = regular_user.notification_preferences
        prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.TELEGRAM]
        prefs.notification_type_settings = {}  # No overrides
        prefs.save()

        # Act
        channels = determine_delivery_channels(regular_user, NotificationType.TICKET_CREATED)

        # Assert - Should use global channels
        assert set(channels) == {DeliveryChannel.IN_APP, DeliveryChannel.TELEGRAM}
        assert DeliveryChannel.EMAIL not in channels
