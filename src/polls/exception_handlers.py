"""Polls exception handlers.

Registered on the global ``NinjaExtraAPI`` from
:meth:`polls.apps.PollsConfig.ready`. Each handler maps a polls-specific
exception to its HTTP status code, keeping controllers free of try/except
boilerplate.

Ninja Extra dispatches exceptions by MRO — most specific handler wins — so
registering ``PollNotOpenError → 423`` here runs BEFORE the global
``ValidationError → 400`` handler defined in :mod:`api.api`.
"""

import typing as t

import structlog
from django.http import HttpRequest
from ninja.responses import Response

from polls.exceptions import (
    PollAnonymityImmutableError,
    PollLifecycleError,
    PollNotEligibleError,
    PollNotOpenError,
    PollQuestionLockedError,
    PollValidationError,
    PollVoteAlreadyCastError,
    PollVoteChangesNotAllowedError,
)
from polls.utils import format_validation_error

logger = structlog.get_logger(__name__)

ExceptionHandler = t.Callable[[HttpRequest, Exception | t.Type[Exception]], Response]


def _make_simple_handler(status: int) -> ExceptionHandler:
    """Build a handler that renders ``exc`` as a ``{detail: ...}`` JSON body."""

    def handler(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
        return Response(status=status, data={"detail": format_validation_error(t.cast(Exception, exc))})

    return handler


def handle_poll_validation(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Render any of the poll *validation-style* errors as 422."""
    return Response(status=422, data={"detail": format_validation_error(t.cast(Exception, exc))})


# Single source of truth for the exception → status mapping.
HANDLERS: dict[type[Exception], ExceptionHandler] = {
    # Lifecycle / state
    PollNotOpenError: _make_simple_handler(423),
    PollLifecycleError: _make_simple_handler(422),
    # Authorization-ish
    PollNotEligibleError: _make_simple_handler(403),
    PollVoteChangesNotAllowedError: _make_simple_handler(403),
    # Conflict
    PollVoteAlreadyCastError: _make_simple_handler(409),
    # Validation (422)
    PollValidationError: handle_poll_validation,
    PollAnonymityImmutableError: handle_poll_validation,
    # Question/section/option lockdown on non-DRAFT polls — semantically a
    # state lock, not a payload validation error.
    PollQuestionLockedError: _make_simple_handler(423),
}


def register() -> None:
    """Install poll exception handlers on the global Ninja API.

    Called from :meth:`polls.apps.PollsConfig.ready`. Imports the global
    ``api`` lazily to avoid AppConfig import-cycle issues.
    """
    from api.api import api

    for exc_type, handler in HANDLERS.items():
        api.add_exception_handler(exc_type, handler)
