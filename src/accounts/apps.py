from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self) -> None:
        """Import signal handlers and register exception handlers and the session validator when the app is ready."""
        import accounts.signals  # noqa: F401
        from accounts.exception_handlers import register as register_exception_handlers
        from accounts.service.tenant_session import is_tenant_session_valid
        from common.auth_base import register_session_validator

        register_exception_handlers()
        register_session_validator(is_tenant_session_valid)
