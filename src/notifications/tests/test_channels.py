"""Tests for notification channel delivery."""

from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.email import EmailChannel
from notifications.service.channels.in_app import InAppChannel
from notifications.service.channels.telegram import TelegramChannel

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


class TestTelegramChannel:
    """Test Telegram notification channel message formatting.

    These tests verify that _format_telegram_message properly escapes
    HTML special characters in notification titles to prevent injection
    into Telegram's HTML parse mode.
    """

    @patch("notifications.service.channels.telegram.get_template")
    def test_format_message_escapes_html_special_chars_in_title(
        self,
        mock_get_template: MagicMock,
        notification: Notification,
    ) -> None:
        """Test that HTML special characters in the title are escaped.

        When a notification title contains <, >, or & characters, they
        must be escaped to prevent them from being interpreted as HTML
        tags by Telegram's parser.
        """
        # Arrange
        notification.title = "Event <script>alert('xss')</script> & more"
        mock_template = MagicMock()
        mock_template.get_telegram_body.return_value = "Some body text"
        mock_get_template.return_value = mock_template

        channel = TelegramChannel()

        # Act
        result = channel._format_telegram_message(notification)

        # Assert
        assert result.startswith("<b>")
        assert "<script>" not in result
        assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in result
        assert "&amp; more" in result

    @patch("notifications.service.channels.telegram.get_template")
    def test_format_message_with_plain_title(
        self,
        mock_get_template: MagicMock,
        notification: Notification,
    ) -> None:
        """Test that a title without special characters renders normally.

        Titles containing only plain text should appear unchanged inside
        the bold tag wrapper.
        """
        # Arrange
        notification.title = "Your Ticket is Confirmed"
        mock_template = MagicMock()
        mock_template.get_telegram_body.return_value = "Body content"
        mock_get_template.return_value = mock_template

        channel = TelegramChannel()

        # Act
        result = channel._format_telegram_message(notification)

        # Assert
        assert "<b>Your Ticket is Confirmed</b>" in result

    @patch("notifications.service.channels.telegram.get_template")
    def test_format_message_fallback_escapes_html_special_chars_in_title(
        self,
        mock_get_template: MagicMock,
        notification: Notification,
    ) -> None:
        """Test that the fallback path also escapes the title.

        When template rendering raises an exception, the method falls
        back to using notification.body directly. The title must still
        be HTML-escaped in this fallback path.
        """
        # Arrange
        notification.title = "Price: 5 < 10 & 10 > 5"
        notification.body = "Fallback body content"
        mock_get_template.side_effect = RuntimeError("Template not found")

        channel = TelegramChannel()

        # Act
        result = channel._format_telegram_message(notification)

        # Assert
        assert "<b>Price: 5 &lt; 10 &amp; 10 &gt; 5</b>" in result
        assert "Fallback body content" in result

    @patch("notifications.service.channels.telegram.get_template")
    def test_format_message_escapes_quotes_in_title(
        self,
        mock_get_template: MagicMock,
        notification: Notification,
    ) -> None:
        """Test that quote characters in the title are escaped.

        Single and double quotes could break HTML attribute values if
        the title were ever used inside an attribute context. The
        html.escape function escapes them by default.
        """
        # Arrange
        notification.title = 'Event "Grand Opening" & Bob\'s Party'
        mock_template = MagicMock()
        mock_template.get_telegram_body.return_value = "Details here"
        mock_get_template.return_value = mock_template

        channel = TelegramChannel()

        # Act
        result = channel._format_telegram_message(notification)

        # Assert
        assert "&quot;" in result or "&#x27;" in result
        assert "<b>" in result
        # The raw quotes should not appear unescaped in the bold title
        title_section = result.split("</b>")[0]
        assert '"Grand Opening"' not in title_section
        assert "&amp;" in title_section

    @patch("notifications.service.channels.telegram.get_template")
    def test_format_message_escapes_heart_emoticon_in_title(
        self,
        mock_get_template: MagicMock,
        notification: Notification,
    ) -> None:
        """Test that <3 (heart emoticon) in org name is escaped in the title.

        This reproduces the exact production failure where an organization
        named "the secret sexpo home parties <3" caused Telegram to reject
        the message with: "can't parse entities: Unsupported start tag 3</b".
        """
        # Arrange
        notification.title = "You're now a member of the secret sexpo home parties <3"
        mock_template = MagicMock()
        mock_template.get_telegram_body.return_value = "Welcome to the org!"
        mock_get_template.return_value = mock_template

        channel = TelegramChannel()

        # Act
        result = channel._format_telegram_message(notification)

        # Assert - <3 must be escaped so Telegram doesn't parse it as a tag
        assert "<3" not in result.split("</b>")[0]
        assert "&lt;3" in result

    @patch("notifications.service.channels.telegram.get_template")
    def test_format_message_includes_rendered_template_body(
        self,
        mock_get_template: MagicMock,
        notification: Notification,
    ) -> None:
        """Test that the rendered template body appears after the title.

        The formatted message should have the bold title first, followed
        by a double newline separator, then the rendered body content.
        """
        # Arrange
        notification.title = "Test Title"
        mock_template = MagicMock()
        mock_template.get_telegram_body.return_value = "**Bold** and _italic_ text"
        mock_get_template.return_value = mock_template

        channel = TelegramChannel()

        # Act
        result = channel._format_telegram_message(notification)

        # Assert
        assert result.startswith("<b>Test Title</b>\n\n")
        # The markdown should have been converted to HTML
        # render_markdown converts **Bold** -> <strong>Bold</strong>
        assert "<strong>Bold</strong>" in result or "<b>Bold</b>" in result

    @patch("notifications.service.channels.telegram.get_template")
    def test_format_message_fallback_uses_notification_body(
        self,
        mock_get_template: MagicMock,
        notification: Notification,
    ) -> None:
        """Test that the fallback path uses notification.body when template fails.

        When the template cannot be rendered, the method should fall back
        to rendering notification.body as markdown and including it after
        the escaped title.
        """
        # Arrange
        notification.title = "Simple Title"
        notification.body = "This is the **fallback** body"
        mock_get_template.side_effect = ValueError("Missing context key")

        channel = TelegramChannel()

        # Act
        result = channel._format_telegram_message(notification)

        # Assert
        assert "<b>Simple Title</b>\n\n" in result
        # The fallback body should be rendered from markdown
        assert "<strong>fallback</strong>" in result or "fallback" in result
