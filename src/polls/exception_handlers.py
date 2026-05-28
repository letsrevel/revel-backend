"""Polls exception handlers.

Registered on the global ``NinjaExtraAPI`` from
:meth:`polls.apps.PollsConfig.ready`. Each entry maps a polls-specific
exception to its HTTP status code, keeping controllers free of try/except
boilerplate.

Ninja Extra dispatches exceptions by MRO — most specific handler wins — so
registering ``PollNotOpenError → 423`` here runs BEFORE the global
``ValidationError → 400`` handler defined in :mod:`api.api`.

The reusable handler factories and the registration loop live in
:mod:`common.exception_handlers`.
"""

from common.exception_handlers import ExceptionHandler, make_simple_handler, register_handlers
from polls.exceptions import (
    PollAnonymityImmutableError,
    PollLifecycleError,
    PollNotEligibleError,
    PollNotOpenError,
    PollQuestionLockedError,
    PollResultsMustBeAnonymousError,
    PollValidationError,
    PollVoteAlreadyCastError,
    PollVoteChangesNotAllowedError,
)

# Single source of truth for the exception → status mapping.
HANDLERS: dict[type[Exception], ExceptionHandler] = {
    # Lifecycle / state
    PollNotOpenError: make_simple_handler(423),
    PollLifecycleError: make_simple_handler(422),
    # Authorization-ish
    PollNotEligibleError: make_simple_handler(403),
    PollVoteChangesNotAllowedError: make_simple_handler(403),
    # Conflict
    PollVoteAlreadyCastError: make_simple_handler(409),
    # Validation (422)
    PollValidationError: make_simple_handler(422),
    PollAnonymityImmutableError: make_simple_handler(422),
    PollResultsMustBeAnonymousError: make_simple_handler(422),
    # Question/section/option lockdown on non-DRAFT polls — semantically a
    # state lock, not a payload validation error.
    PollQuestionLockedError: make_simple_handler(423),
}


def register() -> None:
    """Install poll exception handlers on the global Ninja API.

    Called from :meth:`polls.apps.PollsConfig.ready`. Imports the global
    ``api`` lazily to avoid AppConfig import-cycle issues.
    """
    from api.api import api

    register_handlers(api, HANDLERS)
