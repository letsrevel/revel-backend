"""Notifications app configuration."""

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """Configuration for the notifications app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"
    verbose_name = "Notifications"

    def ready(self) -> None:
        """Import signal handlers when app is ready."""
        import notifications.service.signal_handlers  # noqa: F401

        # Import template modules to register templates
        import notifications.service.templates.event_templates  # noqa: F401
        import notifications.service.templates.follow_templates  # noqa: F401
        import notifications.service.templates.invitation_templates  # noqa: F401
        import notifications.service.templates.membership_templates  # noqa: F401
        import notifications.service.templates.organization_templates  # noqa: F401
        import notifications.service.templates.potluck_templates  # noqa: F401
        import notifications.service.templates.questionnaire_templates  # noqa: F401
        import notifications.service.templates.rsvp_templates  # noqa: F401
        import notifications.service.templates.system_templates  # noqa: F401
        import notifications.service.templates.ticket_templates  # noqa: F401
        import notifications.service.templates.waitlist_templates  # noqa: F401
        import notifications.service.templates.whitelist_templates  # noqa: F401
        import notifications.signals  # noqa: F401
