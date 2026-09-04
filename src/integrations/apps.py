from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "integrations"

    def ready(self) -> None:
        """Populate the provider registry and install the per-app exception handlers."""
        from integrations.exception_handlers import register as register_exception_handlers
        from integrations.registry import populate

        populate()
        register_exception_handlers()
