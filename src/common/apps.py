from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Configuration for the common app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "common"

    def ready(self) -> None:
        """Initialize app-level services.

        Called once Django is fully loaded.
        """
        # Import and initialize observability
        from common.observability import init_profiling, init_tracing

        init_tracing()
        init_profiling()
