"""Base template interface for notifications."""

from abc import ABC, abstractmethod
from typing import Any

from notifications.models import Notification


class NotificationTemplate(ABC):
    """Base class for notification templates."""

    @abstractmethod
    def get_title(self, notification: Notification) -> str:
        """Get notification title (for in-app display).

        Args:
            notification: The notification instance

        Returns:
            Title string
        """
        pass

    @abstractmethod
    def get_body(self, notification: Notification) -> str:
        """Get notification body (for in-app display, markdown).

        Args:
            notification: The notification instance

        Returns:
            Body string (markdown formatted)
        """
        pass

    @abstractmethod
    def get_subject(self, notification: Notification) -> str:
        """Get email subject line.

        Args:
            notification: The notification instance

        Returns:
            Email subject string
        """
        pass

    @abstractmethod
    def get_text_body(self, notification: Notification) -> str:
        """Get email text body.

        Args:
            notification: The notification instance

        Returns:
            Plain text email body
        """
        pass

    def get_html_body(self, notification: Notification) -> str | None:
        """Get email HTML body (optional).

        Args:
            notification: The notification instance

        Returns:
            HTML email body or None
        """
        return None

    def get_attachments(self, notification: Notification) -> dict[str, Any]:
        """Get email attachments.

        Args:
            notification: The notification instance

        Returns:
            Dict of {filename: {content_base64: str, mimetype: str}}
        """
        return {}
