from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    def ready(self) -> None:
        """Register the got_request_exception receiver so admin/non-API 500s are logged (#480)."""
        from api import exception_handlers  # noqa: F401  (import registers the @receiver)
