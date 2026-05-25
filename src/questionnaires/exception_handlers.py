"""Questionnaires exception handlers.

Registered on the global ``NinjaExtraAPI`` from
:meth:`questionnaires.apps.QuestionnairesConfig.ready`. Each entry maps a
questionnaire-specific exception to its HTTP status code, keeping controllers
free of try/except boilerplate.

Ninja Extra dispatches exceptions by MRO — most specific handler wins — so these
app-specific handlers take precedence over the generic ``ValidationError → 400``
global defined in :mod:`api.api`. The reusable handler factories and the
registration loop live in :mod:`common.exception_handlers`.
"""

from django.utils.translation import gettext_lazy as _

from common.exception_handlers import (
    ExceptionHandler,
    make_simple_handler,
    make_static_handler,
    register_handlers,
)
from questionnaires.exceptions import (
    CrossQuestionnaireSubmissionError,
    FileValidationError,
    MissingMandatoryAnswerError,
    QuestionIntegrityError,
    SectionIntegrityError,
)

# Single source of truth for the exception → status mapping.
HANDLERS: dict[type[Exception], ExceptionHandler] = {
    CrossQuestionnaireSubmissionError: make_static_handler(
        400, _("You submitted answers refer to a different questionnaire.")
    ),
    MissingMandatoryAnswerError: make_static_handler(400, _("You are missing mandatory answers.")),
    SectionIntegrityError: make_simple_handler(400),
    QuestionIntegrityError: make_simple_handler(400),
    # Parent class — MRO also covers FileOwnershipError, FileLimitExceededError,
    # InvalidFileMimeTypeError, FileSizeExceededError and DisallowedMimeTypeError.
    FileValidationError: make_simple_handler(400),
}


def register() -> None:
    """Install questionnaires exception handlers on the global Ninja API.

    Called from :meth:`questionnaires.apps.QuestionnairesConfig.ready`. Imports
    the global ``api`` lazily to avoid AppConfig import-cycle issues.
    """
    from api.api import api

    register_handlers(api, HANDLERS)
