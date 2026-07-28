from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self) -> None:
        """Import signal handlers and register exception handlers when the app is ready."""
        import accounts.signals  # noqa: F401
        from accounts.exception_handlers import register as register_exception_handlers

        register_exception_handlers()
