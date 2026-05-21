from django.apps import AppConfig


class PollsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "polls"

    def ready(self) -> None:
        """Import signal receivers so they are registered with Django's dispatcher."""
        import polls.signals  # noqa: F401  (registers receivers)
