"""Django app configuration for wallet pass generation."""

from django.apps import AppConfig


class WalletConfig(AppConfig):
    """Configuration for the wallet app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "wallet"
    verbose_name = "Wallet Passes"

    def ready(self) -> None:
        """Register the per-app exception handlers on the global Ninja API."""
        from wallet.exception_handlers import register as register_exception_handlers

        register_exception_handlers()
