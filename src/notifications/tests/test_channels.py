"""Tests for notification channel delivery."""

from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.email import EmailChannel
from notifications.service.channels.in_app import InAppChannel

pytestmark = pytest.mark.django_db


class TestInAppChannel:
    """Test in-app notification channel."""

    def test_can_deliver_always_returns_true(
        self,
        notification: Notification,
    ) -> None:
        """Test that in-app channel always returns true.

        In-app notifications are already in the database, so delivery
        is just marking them as delivered.
        """
        # Arrange
        channel = InAppChannel()

        # Act
        result = channel.can_deliver(notification)

        # Assert
        assert result is True

    def test_deliver_marks_delivery_as_sent(
        self,
        notification_with_delivery: tuple[Notification, NotificationDelivery],
    ) -> None:
        """Test that deliver marks the delivery as sent."""
        # Arrange
        notification, delivery = notification_with_delivery
        channel = InAppChannel()

        # Act
        result = channel.deliver(notification, delivery)

        # Assert
        assert result is True
        delivery.refresh_from_db()
        assert delivery.status == DeliveryStatus.SENT

    def test_deliver_sets_timestamps(
        self,
        notification_with_delivery: tuple[Notification, NotificationDelivery],
    ) -> None:
        """Test that deliver sets attempted_at and delivered_at timestamps."""
        # Arrange
        notification, delivery = notification_with_delivery
        channel = InAppChannel()

        # Act
        before = timezone.now()
        channel.deliver(notification, delivery)
        after = timezone.now()

        # Assert
        delivery.refresh_from_db()
        assert delivery.attempted_at is not None
        assert delivery.delivered_at is not None
        assert before <= delivery.attempted_at <= after
        assert before <= delivery.delivered_at <= after


class TestEmailChannel:
    """Test email notification channel."""

    def test_can_deliver_checks_email_enabled(
        self,
        notification: Notification,
    ) -> None:
        """Test that can_deliver checks if email channel is enabled."""
        # Arrange
        prefs = notification.user.notification_preferences
        prefs.enabled_channels = [DeliveryChannel.IN_APP]  # Disable email
        prefs.save()

        channel = EmailChannel()

        # Act
        result = channel.can_deliver(notification)

        # Assert
        assert result is False

    def test_can_deliver_checks_notification_type_enabled(
        self,
        notification: Notification,
    ) -> None:
        """Test that can_deliver checks if notification type is enabled."""
        # Arrange
        prefs = notification.user.notification_preferences
        prefs.notification_type_settings = {notification.notification_type: {"enabled": False}}
        prefs.save()

        channel = EmailChannel()

        # Act
        result = channel.can_deliver(notification)

        # Assert
        assert result is False

    def test_can_deliver_checks_user_has_email(
        self,
        notification: Notification,
    ) -> None:
        """Test that can_deliver checks if user has email address."""
        # Arrange
        # Remove user's email
        notification.user.email = ""
        notification.user.save()

        channel = EmailChannel()

        # Act
        result = channel.can_deliver(notification)

        # Assert
        assert result is False

    def test_can_deliver_returns_true_when_all_checks_pass(
        self,
        notification: Notification,
    ) -> None:
        """Test that can_deliver returns true when all checks pass."""
        # Arrange
        channel = EmailChannel()

        # Act
        result = channel.can_deliver(notification)

        # Assert
        assert result is True

    @patch("django.core.mail.EmailMultiAlternatives.send")
    @patch("notifications.service.templates.registry.get_email_template")
    def test_deliver_sends_email_successfully(
        self,
        mock_get_template: MagicMock,
        mock_email_send: MagicMock,
        notification_with_delivery: tuple[Notification, NotificationDelivery],
    ) -> None:
        """Test that deliver sends email via task."""
        # Arrange
        notification, delivery = notification_with_delivery
        delivery.channel = DeliveryChannel.EMAIL
        delivery.save()

        # Mock template
        mock_template = MagicMock()
        mock_template.get_subject.return_value = "Test Subject"
        mock_template.get_text_body.return_value = "Test Body"
        mock_template.get_html_body.return_value = "<p>Test Body</p>"
        mock_template.get_attachments.return_value = {}
        mock_get_template.return_value = mock_template

        # Mock EmailMultiAlternatives.send() to return success
        mock_email_send.return_value = 1

        channel = EmailChannel()

        # Act
        result = channel.deliver(notification, delivery)

        # Assert
        assert result is True
        delivery.refresh_from_db()
        assert delivery.status == DeliveryStatus.SENT
        assert delivery.delivered_at is not None

        # Verify email was sent
        mock_email_send.assert_called_once()

    @patch("django.core.mail.EmailMultiAlternatives.send")
    @patch("notifications.service.templates.registry.get_email_template")
    def test_deliver_handles_smtp_exception(
        self,
        mock_get_template: MagicMock,
        mock_email_send: MagicMock,
        notification_with_delivery: tuple[Notification, NotificationDelivery],
    ) -> None:
        """Test that deliver handles SMTP exceptions gracefully."""
        # Arrange
        notification, delivery = notification_with_delivery
        delivery.channel = DeliveryChannel.EMAIL
        delivery.save()

        # Mock template
        mock_template = MagicMock()
        mock_template.get_subject.return_value = "Test Subject"
        mock_template.get_text_body.return_value = "Test Body"
        mock_template.get_html_body.return_value = "<p>Test Body</p>"
        mock_template.get_attachments.return_value = {}
        mock_get_template.return_value = mock_template

        # Mock EmailMultiAlternatives.send() to raise exception
        mock_email_send.side_effect = Exception("SMTP error")

        channel = EmailChannel()

        # Act
        result = channel.deliver(notification, delivery)

        # Assert
        assert result is False
        delivery.refresh_from_db()
        assert delivery.status == DeliveryStatus.FAILED
        assert "SMTP error" in delivery.error_message
        assert delivery.retry_count == 1
