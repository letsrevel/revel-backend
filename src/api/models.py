"""API models."""

from django.conf import settings


def get_version() -> str:
    """Get the current version of the application."""
    return settings.VERSION
