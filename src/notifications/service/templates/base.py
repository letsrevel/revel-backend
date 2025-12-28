"""Base template interface for notifications."""

import typing as t
from abc import ABC, abstractmethod

from django.template.loader import render_to_string

from notifications.models import Notification
from notifications.utils import get_formatted_context_for_template


class NotificationTemplate(ABC):
    """Base class for notification templates.

    This class provides a channel-aware template interface where each notification
    type must implement rendering for three channels:
    - In-app: Title + markdown body (stored in notification.body, sanitized at save time)
    - Email: Subject + text body + HTML body
    - Telegram: Markdown body (sanitized for Telegram HTML subset)

    The base class provides default implementations that render Django templates
    from the standard structure:
    - notifications/in_app/{notification_type}.md
    - notifications/email/{notification_type}.{txt,html}
    - notifications/telegram/{notification_type}.md
    """

    # ==================== In-App Channel ====================

    @abstractmethod
    def get_in_app_title(self, notification: Notification) -> str:
        """Get title for in-app display.

        Args:
            notification: The notification instance

        Returns:
            Title string
        """
        pass

    def get_in_app_body(self, notification: Notification) -> str:
        """Get markdown body for in-app display.

        By default, renders the template at:
        notifications/in_app/{notification_type}.md

        Args:
            notification: The notification instance

        Returns:
            Markdown body string
        """
        template_name = f"notifications/in_app/{notification.notification_type}.md"
        return render_to_string(template_name, self._get_template_context(notification))

    # ==================== Email Channel ====================

    @abstractmethod
    def get_email_subject(self, notification: Notification) -> str:
        """Get email subject line.

        Args:
            notification: The notification instance

        Returns:
            Email subject string
        """
        pass

    def get_email_text_body(self, notification: Notification) -> str:
        """Get plain text email body.

        By default, renders the template at:
        notifications/email/{notification_type}.txt

        Args:
            notification: The notification instance

        Returns:
            Plain text email body
        """
        template_name = f"notifications/email/{notification.notification_type}.txt"
        return render_to_string(template_name, self._get_template_context(notification))

    def get_email_html_body(self, notification: Notification) -> str | None:
        """Get HTML email body.

        By default, renders the template at:
        notifications/email/{notification_type}.html

        Args:
            notification: The notification instance

        Returns:
            HTML email body or None
        """
        template_name = f"notifications/email/{notification.notification_type}.html"
        return render_to_string(template_name, self._get_template_context(notification))

    def get_email_attachments(self, notification: Notification) -> dict[str, t.Any]:
        """Get email attachments.

        Args:
            notification: The notification instance

        Returns:
            Dict of {filename: {content_base64: str, mimetype: str}}
        """
        return {}

    # ==================== Telegram Channel ====================

    def get_telegram_body(self, notification: Notification) -> str:
        """Get markdown body for Telegram.

        By default, renders the template at:
        notifications/telegram/{notification_type}.md

        The markdown will be converted to HTML via render_markdown() and then
        sanitized for Telegram's HTML subset in the TelegramChannel.

        Args:
            notification: The notification instance

        Returns:
            Markdown body string
        """
        template_name = f"notifications/telegram/{notification.notification_type}.md"
        return render_to_string(template_name, self._get_template_context(notification))

    # ==================== Helper Methods ====================

    def _get_template_context(self, notification: Notification) -> dict[str, t.Any]:
        """Build context for template rendering.

        This method enriches the notification context with:
        - Formatted datetime strings
        - Organization signatures (HTML and markdown)
        - Event links
        - User information
        - Unsubscribe link

        Args:
            notification: The notification instance

        Returns:
            Template context dict with user and enriched context
        """
        from common.models import SiteSettings
        from notifications.service.unsubscribe import generate_unsubscribe_token

        user = notification.user
        user_language = user.language if hasattr(user, "language") else "en"

        # Get formatted context with dates, links, etc.
        enriched_context = get_formatted_context_for_template(
            notification.context,
            user_language=user_language,
        )

        # Generate unsubscribe token and link
        unsubscribe_token = generate_unsubscribe_token(user)
        site_settings = SiteSettings.get_solo()
        unsubscribe_link = f"{site_settings.frontend_base_url}/unsubscribe?token={unsubscribe_token}"
        enriched_context["unsubscribe_link"] = unsubscribe_link

        return {
            "user": user,
            "context": enriched_context,
        }
