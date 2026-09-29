from django.apps import AppConfig


class OauthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "oauth"

    def ready(self) -> None:
        """Install the per-app exception handlers and the settings system checks."""
        from oauth.exception_handlers import register as register_exception_handlers

        register_exception_handlers()
        # Importing the module runs its @register() decorators.
        import oauth.checks  # noqa: F401
