"""Template registry for notifications."""

from notifications.enums import NotificationType
from notifications.service.templates.base import NotificationTemplate


class TemplateRegistry:
    """Registry for notification templates."""

    def __init__(self) -> None:
        """Initialize template registry."""
        self._templates: dict[NotificationType, NotificationTemplate] = {}

    def register(self, notification_type: NotificationType, template: NotificationTemplate) -> None:
        """Register a template for a notification type.

        Args:
            notification_type: Type of notification
            template: Template instance
        """
        self._templates[notification_type] = template

    def get(self, notification_type: NotificationType | str) -> NotificationTemplate:
        """Get template for notification type.

        Args:
            notification_type: Type of notification

        Returns:
            Template instance

        Raises:
            ValueError: If no template is registered
        """
        # Convert string to enum if needed
        if isinstance(notification_type, str):
            notification_type = NotificationType(notification_type)

        template = self._templates.get(notification_type)
        if not template:
            raise ValueError(f"No template registered for {notification_type}")
        return template

    def is_registered(self, notification_type: NotificationType | str) -> bool:
        """Check if a template is registered.

        Args:
            notification_type: Type of notification

        Returns:
            True if template is registered
        """
        if isinstance(notification_type, str):
            notification_type = NotificationType(notification_type)
        return notification_type in self._templates


# Global registry instance
_registry = TemplateRegistry()


def register_template(notification_type: NotificationType, template: NotificationTemplate) -> None:
    """Register a template in the global registry.

    Args:
        notification_type: Type of notification
        template: Template instance
    """
    _registry.register(notification_type, template)


def get_template(notification_type: NotificationType | str) -> NotificationTemplate:
    """Get notification template from global registry.

    Args:
        notification_type: Type of notification

    Returns:
        Template instance
    """
    return _registry.get(notification_type)


def get_email_template(notification_type: NotificationType | str) -> NotificationTemplate:
    """Alias for get_template (used by email channel).

    Args:
        notification_type: Type of notification

    Returns:
        Template instance
    """
    return get_template(notification_type)


def is_template_registered(notification_type: NotificationType | str) -> bool:
    """Check if a template is registered.

    Args:
        notification_type: Type of notification

    Returns:
        True if template is registered
    """
    return _registry.is_registered(notification_type)
