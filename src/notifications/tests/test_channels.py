"""Tests for notification channel delivery."""

from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.email import EmailChannel
from notifications.service.channels.in_app import InAppChannel
from notifications.service.channels.telegram import TelegramChannel
from notifications.utils import (
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_MESSAGE_LIMIT,
    truncate_telegram_html,
)

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

    @patch("notifications.service.channels.telegram.send_message_task")
    @patch("notifications.service.channels.telegram.get_notification_keyboard")
    @patch("notifications.service.channels.telegram.get_template")
    def test_deliver_truncates_long_message(
        self,
        mock_get_template: MagicMock,
        mock_get_keyboard: MagicMock,
        mock_send_task: MagicMock,
        notification_with_delivery: tuple[Notification, NotificationDelivery],
    ) -> None:
        """Test that deliver() truncates messages exceeding the 4096 limit."""
        notification, delivery = notification_with_delivery
        delivery.channel = DeliveryChannel.TELEGRAM
        delivery.metadata = {}
        delivery.save()

        # Connect a fake telegram account
        mock_tg_manager = MagicMock()
        tg_user = MagicMock()
        tg_user.telegram_id = 123456
        mock_tg_manager.filter.return_value.first.return_value = tg_user
        notification.user.notification_preferences.enabled_channels = [
            DeliveryChannel.IN_APP,
            DeliveryChannel.TELEGRAM,
        ]
        notification.user.notification_preferences.save()

        # Build a body that exceeds 4096 chars
        long_body = "A" * 5000
        mock_template = MagicMock()
        mock_template.get_telegram_body.return_value = long_body
        mock_get_template.return_value = mock_template
        mock_get_keyboard.return_value = None
        mock_send_task.delay.return_value = MagicMock(id="task-id")

        channel = TelegramChannel()
        with patch.object(
            type(notification.user), "telegram_users", new_callable=lambda: property(lambda self: mock_tg_manager)
        ):
            channel.deliver(notification, delivery)

        # The message passed to send_message_task must be within the limit
        assert mock_send_task.delay.call_args is not None
        sent_message: str = mock_send_task.delay.call_args.kwargs["message"]
        assert len(sent_message) <= TELEGRAM_MESSAGE_LIMIT

    @patch("notifications.service.channels.telegram.send_message_task")
    @patch("notifications.service.channels.telegram.get_notification_keyboard")
    @patch("notifications.service.channels.telegram.get_template")
    def test_deliver_uses_caption_limit_for_photo_messages(
        self,
        mock_get_template: MagicMock,
        mock_get_keyboard: MagicMock,
        mock_send_task: MagicMock,
        notification_with_delivery: tuple[Notification, NotificationDelivery],
    ) -> None:
        """Test that deliver() uses 1024 limit when QR code is attached."""
        notification, delivery = notification_with_delivery
        delivery.channel = DeliveryChannel.TELEGRAM
        delivery.metadata = {}
        delivery.save()

        # Make this a ticket notification so _get_qr_data returns a ticket ID
        notification.notification_type = "ticket_created"
        notification.context["ticket_id"] = "some-ticket-id"
        # Remove ticket_holder_name so it's treated as holder notification
        notification.context.pop("ticket_holder_name", None)
        notification.save()

        mock_tg_manager = MagicMock()
        tg_user = MagicMock()
        tg_user.telegram_id = 123456
        mock_tg_manager.filter.return_value.first.return_value = tg_user
        notification.user.notification_preferences.enabled_channels = [
            DeliveryChannel.IN_APP,
            DeliveryChannel.TELEGRAM,
        ]
        notification.user.notification_preferences.save()

        # Build a body that exceeds 1024 but is under 4096
        long_body = "B" * 2000
        mock_template = MagicMock()
        mock_template.get_telegram_body.return_value = long_body
        mock_get_template.return_value = mock_template
        mock_get_keyboard.return_value = None
        mock_send_task.delay.return_value = MagicMock(id="task-id")

        channel = TelegramChannel()
        with patch.object(
            type(notification.user), "telegram_users", new_callable=lambda: property(lambda self: mock_tg_manager)
        ):
            channel.deliver(notification, delivery)

        assert mock_send_task.delay.call_args is not None
        sent_message: str = mock_send_task.delay.call_args.kwargs["message"]
        assert len(sent_message) <= TELEGRAM_CAPTION_LIMIT

    @patch("notifications.service.channels.telegram.send_message_task")
    @patch("notifications.service.channels.telegram.get_notification_keyboard")
    @patch("notifications.service.channels.telegram.get_template")
    def test_deliver_does_not_truncate_short_message(
        self,
        mock_get_template: MagicMock,
        mock_get_keyboard: MagicMock,
        mock_send_task: MagicMock,
        notification_with_delivery: tuple[Notification, NotificationDelivery],
    ) -> None:
        """Test that deliver() does not modify messages under the limit."""
        notification, delivery = notification_with_delivery
        delivery.channel = DeliveryChannel.TELEGRAM
        delivery.metadata = {}
        delivery.save()

        mock_tg_manager = MagicMock()
        tg_user = MagicMock()
        tg_user.telegram_id = 123456
        mock_tg_manager.filter.return_value.first.return_value = tg_user
        notification.user.notification_preferences.enabled_channels = [
            DeliveryChannel.IN_APP,
            DeliveryChannel.TELEGRAM,
        ]
        notification.user.notification_preferences.save()

        short_body = "Hello world"
        mock_template = MagicMock()
        mock_template.get_telegram_body.return_value = short_body
        mock_get_template.return_value = mock_template
        mock_get_keyboard.return_value = None
        mock_send_task.delay.return_value = MagicMock(id="task-id")

        channel = TelegramChannel()
        with patch.object(
            type(notification.user), "telegram_users", new_callable=lambda: property(lambda self: mock_tg_manager)
        ):
            channel.deliver(notification, delivery)

        assert mock_send_task.delay.call_args is not None
        sent_message: str = mock_send_task.delay.call_args.kwargs["message"]
        # Short message should not contain "Read more" link
        assert "Read more" not in sent_message


class TestTruncateTelegramHtml:
    """Unit tests for the truncate_telegram_html utility."""

    def test_noop_when_under_limit(self) -> None:
        """Message under the limit is returned unchanged."""
        message = "<b>Hello</b> world"
        result = truncate_telegram_html(message, max_length=100, suffix="...")
        assert result == message

    def test_truncates_and_appends_suffix(self) -> None:
        """Long message is truncated and suffix is appended."""
        message = "A" * 200
        suffix = "..."
        result = truncate_telegram_html(message, max_length=100, suffix=suffix)
        assert len(result) <= 100
        assert result.endswith(suffix)

    def test_does_not_cut_inside_html_tag(self) -> None:
        """Truncation point should not land inside an HTML tag."""
        # Place a tag right at the cut boundary
        message = "A" * 90 + "<b>bold text</b>" + "A" * 100
        suffix = "..."
        result = truncate_telegram_html(message, max_length=100, suffix=suffix)
        # The result must not contain a partial tag like "<b" without ">"
        assert "<b" not in result or ("<b>" in result or "<b " in result)
        assert len(result) <= 100

    def test_does_not_cut_inside_html_entity(self) -> None:
        """Truncation point should not land inside an HTML entity."""
        # Place an entity right at the cut boundary
        message = "A" * 93 + "&amp;" + "A" * 100
        suffix = "..."
        result = truncate_telegram_html(message, max_length=100, suffix=suffix)
        # Should not contain a partial entity like "&amp" without ";"
        assert "&amp" not in result or "&amp;" in result
        assert len(result) <= 100

    def test_closes_unclosed_tags(self) -> None:
        """Unclosed tags in truncated text are closed properly."""
        message = "<b>This is bold and <i>italic text that goes on" + "x" * 200
        suffix = "..."
        result = truncate_telegram_html(message, max_length=60, suffix=suffix)
        assert result.count("</i>") >= 1 or "<i>" not in result
        assert result.count("</b>") >= 1 or "<b>" not in result

    def test_already_closed_tags_not_double_closed(self) -> None:
        """Tags that are already closed should not be closed again."""
        message = "<b>bold</b> <i>italic</i> " + "A" * 200
        suffix = "..."
        result = truncate_telegram_html(message, max_length=50, suffix=suffix)
        # </b> and </i> already present — should not add extra closings
        assert result.count("</b>") <= 1
        assert result.count("</i>") <= 1

    def test_closes_nested_tags_in_correct_order(self) -> None:
        """Nested tags are closed in the correct order when truncating."""
        prefix = "<b>outer <i>inner <b>deep</b> text"
        long_tag = '<a href="' + ("x" * 50) + '">link</a>'
        message = prefix + long_tag + " tail" + ("x" * 200)
        suffix = "..."
        # Target falls inside the <a ...> opening tag, so the cut stays at prefix.
        max_length = len(prefix) + len(suffix) + 20

        result = truncate_telegram_html(message, max_length=max_length, suffix=suffix)

        assert result.endswith("</i></b>...")

    def test_suffix_with_html_link(self) -> None:
        """Realistic suffix with an HTML link works correctly."""
        message = "A" * 5000
        suffix = '\n\n<a href="https://example.com/notifications">Read more...</a>'
        result = truncate_telegram_html(message, max_length=TELEGRAM_MESSAGE_LIMIT, suffix=suffix)
        assert len(result) <= TELEGRAM_MESSAGE_LIMIT
        assert "Read more..." in result

    def test_exact_limit_not_truncated(self) -> None:
        """Message exactly at the limit is not truncated."""
        message = "A" * 4096
        result = truncate_telegram_html(message, max_length=4096, suffix="...")
        assert result == message

    def test_unsupported_tags_not_closed(self) -> None:
        """Only Telegram-supported tags are closed; unsupported ones are ignored."""
        # Simulate sanitize_for_telegram leaving a stray <li> opener in the text
        message = "<b>Title</b>\n\n<li>item text" + "x" * 200
        suffix = '…\n\n<a href="https://example.com">Read more...</a>'
        result = truncate_telegram_html(message, max_length=80, suffix=suffix)
        # </b> should be closed if needed, but </li> must NOT appear
        assert "</li>" not in result
        assert "Read more..." in result

    def test_overshoot_retry_does_not_cut_inside_tag(self) -> None:
        """Overshoot retry must not produce malformed HTML by cutting inside a tag.

        When closing tags push the first-pass result over the limit, the retry
        must still use _find_safe_cut_point to avoid landing inside an <a href>
        tag, which would break Telegram's HTML parser and strip all formatting.
        """
        # Craft a message where:
        # 1. The first-pass cut lands just after an <a> tag with a long href
        # 2. Closing markup (</a>) pushes the result over the limit
        # 3. The overshoot retry would land INSIDE the <a href="..."> if not using safe cut
        long_url = "https://example.com/" + "p" * 60
        a_tag = f'<a href="{long_url}">link text'  # intentionally unclosed at cut point
        padding_before = "x" * 40
        padding_after = "y" * 200
        message = padding_before + a_tag + padding_after
        suffix = '…\n\n<a href="https://example.com">Read more...</a>'

        # Choose max_length so the first-pass cut lands right after the <a> opening tag,
        # and closing </a> causes overshoot that would land inside <a href="...">
        max_length = len(padding_before) + len(a_tag) + len(suffix) + 5

        result = truncate_telegram_html(message, max_length, suffix)

        # The result must NOT contain a partial/malformed <a tag
        # Check there are no '<a' without a matching '>' before the next '<'
        import re

        partial_tags = re.findall(r"<a\s[^>]*(?=<|$)", result)
        assert not partial_tags, f"Found malformed <a> tag in: {result}"

        # The suffix link must be present and well-formed
        assert "Read more..." in result
        assert result.endswith("</a>")
