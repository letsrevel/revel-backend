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
    CrossQuestionnaireOptionDependencyError,
    CrossQuestionnaireSectionError,
    CrossQuestionnaireSubmissionError,
    DisallowedMultipleAnswersError,
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
    FileValidationError: make_simple_handler(422),
    # Model ``clean()`` invariants. Two reasons these don't surface as their own
    # type over HTTP today: (1) the service shadows them (``_resolve_question_dependencies``
    # raises Section/QuestionIntegrityError first, and answers are persisted via
    # ``bulk_create`` which bypasses ``full_clean``); (2) even when reached,
    # ``Model.full_clean()`` re-wraps a ``clean()`` ValidationError as a *generic*
    # ``ValidationError`` (subclass identity lost), so the global handler answers.
    # Registered for future-proofing — they only win if raised directly. They carry
    # ``ValidationError`` content, so render it.
    CrossQuestionnaireSectionError: make_simple_handler(400),
    CrossQuestionnaireOptionDependencyError: make_simple_handler(400),
    DisallowedMultipleAnswersError: make_simple_handler(409),
}


def register() -> None:
    """Install questionnaires exception handlers on the global Ninja API.

    Called from :meth:`questionnaires.apps.QuestionnairesConfig.ready`. Imports
    the global ``api`` lazily to avoid AppConfig import-cycle issues.
    """
    from api.api import api

    register_handlers(api, HANDLERS)
