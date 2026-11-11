"""Resolve the language an email recipient should be addressed in."""

import typing as t

from django.conf import settings

if t.TYPE_CHECKING:
    from accounts.models import RevelUser


def _supported(language: str | None) -> str | None:
    """Return ``language`` if it is one of ``settings.LANGUAGES``, else ``None``."""
    return language if language and language in dict(settings.LANGUAGES) else None


def is_supported_language(language: str | None) -> bool:
    """Whether ``language`` is one of ``settings.LANGUAGES``."""
    return _supported(language) is not None


def recipient_language(email: str | None = None, *, user: "RevelUser | None" = None) -> str:
    """Return the language an email recipient should receive their message in.

    Celery workers have no active language, so any task rendering an email must resolve it
    explicitly and wrap rendering in ``translation.override(...)``.

    Args:
        email: Recipient address; matched case-insensitively against ``RevelUser.email``
            (guest users included). Ignored when ``user`` is given.
        user: The recipient, when already known.

    Returns:
        The user's language if it is a supported one, otherwise ``settings.LANGUAGE_CODE``.
    """
    from accounts.models import RevelUser

    if user is None and email:
        user = RevelUser.objects.filter(email__iexact=email).only("language").first()
    return _supported(user.language if user else None) or settings.LANGUAGE_CODE
