from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "events"

    def ready(self) -> None:
        """Import signals/tasks and register the per-app exception handlers."""
        from . import signals, tasks_stripe  # noqa: F401
        from .exception_handlers import register as register_exception_handlers

        register_exception_handlers()
