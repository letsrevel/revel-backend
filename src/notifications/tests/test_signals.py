"""Tests for notification signal handlers."""

import pytest

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import NotificationPreference

pytestmark = pytest.mark.django_db


class TestCreateNotificationPreferences:
    """Test automatic creation of notification preferences on user creation."""

    def test_creates_preferences_for_regular_user(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Test that notification preferences are created for regular users.

        Regular users should have both IN_APP and EMAIL channels enabled,
        with no notification type restrictions (empty notification_type_settings).
        """
        # Act - Creating user triggers signal
        user = django_user_model.objects.create_user(
            username="test@example.com",
            email="test@example.com",
            password="password",
            guest=False,
        )

        # Assert - Preferences created automatically
        assert hasattr(user, "notification_preferences")
        prefs = user.notification_preferences

        assert prefs is not None
        assert DeliveryChannel.IN_APP in prefs.enabled_channels
        assert DeliveryChannel.EMAIL in prefs.enabled_channels

        # Regular users have no restrictions
        assert len(prefs.notification_type_settings) == 4  # potluck email notifications are disabled.

    def test_creates_preferences_for_guest_user_with_restrictions(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Test that guest users get restricted notification types.

        Guest users get 15 notification types disabled (only event participation
        essentials like TICKET_CREATED, EVENT_REMINDER, etc. remain enabled).
        """
        # Act
        user = django_user_model.objects.create_user(
            username="guest@example.com",
            email="guest@example.com",
            password="password",
            guest=True,
        )

        # Assert
        prefs = user.notification_preferences

        # Guest users still get both channels
        assert DeliveryChannel.IN_APP in prefs.enabled_channels
        assert DeliveryChannel.EMAIL in prefs.enabled_channels

        # But have 15 notification types disabled
        assert len(prefs.notification_type_settings) == 15

        # Verify specific disabled types
        disabled_types = [
            NotificationType.EVENT_OPEN,
            NotificationType.EVENT_CREATED,
            NotificationType.POTLUCK_ITEM_CREATED,
            NotificationType.POTLUCK_ITEM_UPDATED,
            NotificationType.POTLUCK_ITEM_CLAIMED,
            NotificationType.POTLUCK_ITEM_UNCLAIMED,
            NotificationType.QUESTIONNAIRE_SUBMITTED,
            NotificationType.INVITATION_CLAIMED,
            NotificationType.MEMBERSHIP_GRANTED,
            NotificationType.MEMBERSHIP_PROMOTED,
            NotificationType.MEMBERSHIP_REMOVED,
            NotificationType.MEMBERSHIP_REQUEST_APPROVED,
            NotificationType.MEMBERSHIP_REQUEST_REJECTED,
            NotificationType.ORG_ANNOUNCEMENT,
            NotificationType.MALWARE_DETECTED,
        ]

        for notif_type in disabled_types:
            assert notif_type in prefs.notification_type_settings
            assert prefs.notification_type_settings[notif_type]["enabled"] is False

    def test_handles_duplicate_signal_with_get_or_create(
        self,
        django_user_model: type[RevelUser],
    ) -> None:
        """Test that duplicate signal calls don't cause integrity errors.

        The signal uses get_or_create to handle race conditions where
        the signal might be called multiple times.
        """
        # Create user (triggers signal)
        user = django_user_model.objects.create_user(
            username="duplicate@example.com",
            email="duplicate@example.com",
            password="password",
        )

        # Manually try to create again (simulating duplicate signal)
        prefs, created = NotificationPreference.objects.get_or_create(
            user=user,
            defaults={
                "enabled_channels": [DeliveryChannel.IN_APP],
            },
        )

        # Should not create duplicate
        assert created is False
        assert prefs.user == user

        # Should only have one preference
        assert NotificationPreference.objects.filter(user=user).count() == 1
