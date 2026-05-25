"""Shared building blocks for per-app exception handlers.

Each Django app self-registers its exception handlers on the global
``NinjaExtraAPI`` from its ``AppConfig.ready`` hook, mapping an exception type
to an HTTP status code in one place. This module holds the reusable pieces so
every app's ``exception_handlers.py`` stays a thin declarative table:

- :func:`format_validation_error` — flatten a Django ``ValidationError`` (or any
  exception) into a single-line string.
- :func:`make_simple_handler` — render ``str(exc)`` as ``{"detail": ...}`` (for
  exceptions raised *with* a meaningful message or ``ValidationError`` content).
- :func:`make_static_handler` — render a fixed, translatable message as
  ``{"detail": ...}`` (for exceptions raised bare, where ``str(exc)`` is empty).
- :func:`register_handlers` — install a ``{exc_type: handler}`` mapping on the API.

Ninja Extra dispatches exceptions by MRO — most specific registered handler
wins — so app-specific handlers take precedence over the generic
``ValidationError → 400`` global defined in :mod:`api.api`.

Kept deliberately lightweight (Django + ninja only, no models/Celery imports) so
it is safe to import from an ``AppConfig.ready`` hook.
"""

import typing as t

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest
from django.utils.functional import Promise
from ninja.responses import Response

ExceptionHandler = t.Callable[[HttpRequest, Exception | t.Type[Exception]], Response]


class SupportsExceptionHandlerRegistration(t.Protocol):
    """Structural type for the subset of ``NinjaExtraAPI`` we depend on."""

    def add_exception_handler(self, exc_class: type[Exception], handler: ExceptionHandler) -> None:
        """Register ``handler`` for ``exc_class`` on the API."""
        ...


def format_validation_error(exc: DjangoValidationError | Exception) -> str:
    """Render a Django ``ValidationError`` (or arbitrary exception) into a single-line string.

    Prefers ``message_dict`` when present (``full_clean`` / ``validate_constraints``
    output), falls back to ``messages`` (string-only constructions), and finally
    to ``str(exc)``.

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


def make_simple_handler(status: int) -> ExceptionHandler:
    """Build a handler that renders ``exc`` as a ``{"detail": ...}`` JSON body.

    Use for exceptions raised with a meaningful message or ``ValidationError``
    content (see :func:`format_validation_error`).
    """

    def handler(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
        return Response(status=status, data={"detail": format_validation_error(t.cast(Exception, exc))})

    return handler


def make_static_handler(status: int, message: str | Promise) -> ExceptionHandler:
    """Build a handler that renders a fixed ``message`` as a ``{"detail": ...}`` JSON body.

    Use for exceptions raised bare (``raise FooError``) where ``str(exc)`` is
    empty. ``message`` may be a lazy translation proxy; it is stringified at
    request time.
    """

    def handler(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
        return Response(status=status, data={"detail": str(message)})

    return handler


def register_handlers(
    api: SupportsExceptionHandlerRegistration,
    handlers: dict[type[Exception], ExceptionHandler],
) -> None:
    """Install a ``{exc_type: handler}`` mapping on the given Ninja API.

    Called from each app's ``AppConfig.ready`` (which imports the global ``api``
    lazily to avoid import-cycle issues).
    """
    for exc_type, handler in handlers.items():
        api.add_exception_handler(exc_type, handler)
