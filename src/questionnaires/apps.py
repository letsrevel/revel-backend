from django.apps import AppConfig


class QuestionnairesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "questionnaires"

    def ready(self) -> None:
        """Register the per-app exception handlers."""
        from questionnaires.exception_handlers import register as register_exception_handlers

        register_exception_handlers()
