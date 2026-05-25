"""Utilities for the polls app."""

from django.core.exceptions import ValidationError as DjangoValidationError


def format_validation_error(exc: DjangoValidationError | Exception) -> str:
    """Render a Django ``ValidationError`` (or arbitrary exception) into a single-line string.

    Prefers ``message_dict`` when present (full_clean / validate_constraints
    output), falls back to ``messages`` (string-only constructions like our
    poll-specific subclasses), and finally to ``str(exc)``.

    Args:
        exc: A ``DjangoValidationError`` or any other ``Exception``.

    Returns:
        A flat string suitable for embedding in an HTTP error response.
    """
    if isinstance(exc, DjangoValidationError):
        if hasattr(exc, "message_dict"):
            return "; ".join(f"{field}: {', '.join(map(str, msgs))}" for field, msgs in exc.message_dict.items())
        if hasattr(exc, "messages"):
            return "; ".join(str(m) for m in exc.messages)
    return str(exc)
