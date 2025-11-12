"""Tests for notification digest functionality."""

from datetime import time, timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery, NotificationPreference
from notifications.service.digest import (
    NotificationDigest,
    get_digest_lookback_period,
    get_pending_notifications_for_digest,
    should_send_digest_now,
)

pytestmark = pytest.mark.django_db


class TestGetPendingNotificationsForDigest:
    """Test getting notifications pending digest delivery."""

    def test_returns_unread_notifications_without_email_delivery(
        self,
        digest_notifications: list[Notification],
    ) -> None:
        """Test that only unread notifications without email delivery are returned."""
        # Arrange - All notifications are unread and have no email delivery yet
        since = timezone.now() - timedelta(hours=3)

        # Act
        pending = get_pending_notifications_for_digest(digest_notifications[0].user, since)

        # Assert
        assert pending.count() == 3

    def test_excludes_notifications_with_email_delivery(
        self,
        digest_notifications: list[Notification],
    ) -> None:
        """Test that notifications with successful email delivery are excluded."""
        # Arrange - Mark one as having email delivery
        NotificationDelivery.objects.create(
            notification=digest_notifications[0],
            channel=DeliveryChannel.EMAIL,
            status=DeliveryStatus.SENT,
        )

        since = timezone.now() - timedelta(hours=3)

        # Act
        pending = get_pending_notifications_for_digest(digest_notifications[0].user, since)

        # Assert - Should only get 2 notifications (one has email delivery)
        assert pending.count() == 2
        assert digest_notifications[0] not in pending

    def test_excludes_read_notifications(
        self,
        digest_notifications: list[Notification],
    ) -> None:
        """Test that read notifications are excluded."""
        # Arrange - Mark one as read
        digest_notifications[0].read_at = timezone.now()
        digest_notifications[0].save()

        since = timezone.now() - timedelta(hours=3)

        # Act
        pending = get_pending_notifications_for_digest(digest_notifications[0].user, since)

        # Assert
        assert pending.count() == 2
        assert digest_notifications[0] not in pending

    def test_respects_since_timestamp(
        self,
        digest_notifications: list[Notification],
    ) -> None:
        """Test that only notifications after 'since' are included."""
        # Arrange - Set 'since' to after first notification
        since = digest_notifications[0].created_at + timedelta(minutes=5)

        # Act
        pending = get_pending_notifications_for_digest(digest_notifications[0].user, since)

        # Assert - Should only get last 2 notifications
        assert pending.count() == 2
        assert digest_notifications[0] not in pending


class TestShouldSendDigestNow:
    """Test digest send timing logic."""

    def test_returns_false_for_immediate_mode(
        self,
        regular_user: RevelUser,
    ) -> None:
        """Test that immediate mode users don't get digests."""
        # Arrange - User in immediate mode
        prefs = regular_user.notification_preferences
        assert prefs.digest_frequency == NotificationPreference.DigestFrequency.IMMEDIATE

        # Act
        result = should_send_digest_now(regular_user)

        # Assert
        assert result is False

    @patch("notifications.service.digest.timezone")
    def test_returns_true_when_within_time_window(
        self,
        mock_timezone: MagicMock,
        regular_user: RevelUser,
    ) -> None:
        """Test that digest is sent when current time matches user's preferred time.

        The function allows a 30-minute window around the preferred send time.
        """
        # Arrange - Set user to daily digest at 9:00 AM
        prefs = regular_user.notification_preferences
        prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
        prefs.digest_send_time = time(9, 0)
        prefs.save()

        # Mock current time to be 9:15 AM (within 30 min window)
        mock_now = timezone.now().replace(hour=9, minute=15)
        mock_timezone.now.return_value = mock_now
        mock_timezone.localtime.return_value = mock_now

        # Act
        result = should_send_digest_now(regular_user)

        # Assert
        assert result is True

    @patch("notifications.service.digest.timezone")
    def test_returns_false_when_outside_time_window(
        self,
        mock_timezone: MagicMock,
        regular_user: RevelUser,
    ) -> None:
        """Test that digest is not sent when outside the time window."""
        # Arrange - Set user to daily digest at 9:00 AM
        prefs = regular_user.notification_preferences
        prefs.digest_frequency = NotificationPreference.DigestFrequency.DAILY
        prefs.digest_send_time = time(9, 0)
        prefs.save()

        # Mock current time to be 11:00 AM (more than 30 min away)
        mock_now = timezone.now().replace(hour=11, minute=0)
        mock_timezone.now.return_value = mock_now
        mock_timezone.localtime.return_value = mock_now

        # Act
        result = should_send_digest_now(regular_user)

        # Assert
        assert result is False

    @patch("notifications.service.digest.timezone")
    def test_weekly_digest_only_sends_on_monday(
        self,
        mock_timezone: MagicMock,
        regular_user: RevelUser,
    ) -> None:
        """Test that weekly digests only send on Mondays."""
        # Arrange - Set user to weekly digest
        prefs = regular_user.notification_preferences
        prefs.digest_frequency = NotificationPreference.DigestFrequency.WEEKLY
        prefs.digest_send_time = time(9, 0)
        prefs.save()

        # Mock current time to Tuesday at 9:00 AM
        mock_now = timezone.now()
        mock_now = mock_now.replace(hour=9, minute=0)
        # Set to Tuesday (weekday() == 1)
        days_ahead = 1 - mock_now.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        mock_now = mock_now + timedelta(days=days_ahead)
        mock_timezone.now.return_value = mock_now
        mock_timezone.localtime.return_value = mock_now

        # Act
        result = should_send_digest_now(regular_user)

        # Assert - Should be false (not Monday)
        assert result is False


class TestNotificationDigest:
    """Test digest building and sending."""

    @patch("common.tasks.send_email.delay")
    def test_send_digest_email_triggers_task(
        self,
        mock_send_email: MagicMock,
        regular_user: RevelUser,
        digest_notifications: list[Notification],
    ) -> None:
        """Test that digest email triggers the send_email task."""
        # Arrange
        digest = NotificationDigest(regular_user, Notification.objects.filter(user=regular_user))

        # Act
        result = digest.send_digest_email()

        # Assert
        assert result is True
        mock_send_email.assert_called_once()
        call_kwargs = mock_send_email.call_args.kwargs
        assert call_kwargs["to"] == regular_user.email
        assert "notification" in call_kwargs["subject"].lower()

    def test_build_digest_content_groups_by_type(
        self,
        regular_user: RevelUser,
        digest_notifications: list[Notification],
    ) -> None:
        """Test that digest content groups notifications by type."""
        # Arrange
        digest = NotificationDigest(regular_user, Notification.objects.filter(user=regular_user))

        # Act
        subject, text_body, html_body = digest.build_digest_content()

        # Assert
        assert "3 new notification" in subject
        # Check that the notification type appears in the digest
        assert "EVENT_REMINDER" in text_body or "event_reminder" in text_body
        assert len(text_body) > 0
        assert len(html_body) > 0


class TestGetDigestLookbackPeriod:
    """Test lookback period calculation."""

    def test_hourly_returns_one_hour(self) -> None:
        """Test that hourly frequency returns 1 hour lookback."""
        period = get_digest_lookback_period(NotificationPreference.DigestFrequency.HOURLY)
        assert period == timedelta(hours=1)

    def test_daily_returns_one_day(self) -> None:
        """Test that daily frequency returns 1 day lookback."""
        period = get_digest_lookback_period(NotificationPreference.DigestFrequency.DAILY)
        assert period == timedelta(days=1)

    def test_weekly_returns_one_week(self) -> None:
        """Test that weekly frequency returns 1 week lookback."""
        period = get_digest_lookback_period(NotificationPreference.DigestFrequency.WEEKLY)
        assert period == timedelta(weeks=1)

    def test_raises_error_for_invalid_frequency(self) -> None:
        """Test that invalid frequency raises ValueError."""
        with pytest.raises(ValueError, match="Invalid digest frequency"):
            get_digest_lookback_period("invalid")
