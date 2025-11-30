"""Django app configuration for wallet pass generation."""

from django.apps import AppConfig


class WalletConfig(AppConfig):
    """Configuration for the wallet app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "wallet"
    verbose_name = "Wallet Passes"
