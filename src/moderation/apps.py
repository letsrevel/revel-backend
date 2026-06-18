from django.apps import AppConfig


class ModerationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "moderation"

    def ready(self) -> None:
        """Register moderation exception handlers on the global Ninja API."""
        from moderation.exception_handlers import register as register_exception_handlers

        register_exception_handlers()
