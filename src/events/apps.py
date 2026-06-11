from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "events"

    def ready(self) -> None:
        """Run startup-time side effects for the events app.

        Imports the signal handlers and the ``tasks_stripe`` module (so the
        Celery worker registers its tasks) and installs the per-app exception
        handlers on the global Ninja API.

        Returns:
            None: Performs registration side effects only.
        """
        from . import signals, tasks_stripe  # noqa: F401
        from .exception_handlers import register as register_exception_handlers

        register_exception_handlers()
