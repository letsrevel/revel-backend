"""Unit tests for the questionnaires exception → HTTP status mapping.

The three model ``clean()`` errors below are shadowed at the service layer
(``_resolve_question_dependencies`` raises ``Section``/``QuestionIntegrityError``
first, and answers are persisted via ``bulk_create`` which bypasses
``full_clean``), so they do not currently surface through an HTTP endpoint as
their own type. The handlers are registered for future-proofing — these tests
assert the mapping directly against ``HANDLERS`` rather than via a controller.

``FileValidationError`` *is* reachable over HTTP; its 422 mapping is also covered
end-to-end in ``events.tests...test_questionnaire_file_upload``.
"""

import json
import typing as t

from django.http import HttpRequest

from questionnaires.exception_handlers import HANDLERS
from questionnaires.exceptions import (
    CrossQuestionnaireOptionDependencyError,
    CrossQuestionnaireSectionError,
    DisallowedMultipleAnswersError,
    FileValidationError,
)

# The handlers ignore the request, so a typed ``None`` keeps mypy happy.
_REQUEST = t.cast(HttpRequest, None)


def test_cross_questionnaire_section_error_maps_to_400() -> None:
    response = HANDLERS[CrossQuestionnaireSectionError](_REQUEST, CrossQuestionnaireSectionError("bad section"))
    assert response.status_code == 400
    assert json.loads(response.content) == {"detail": "bad section"}


def test_cross_questionnaire_option_dependency_error_maps_to_400() -> None:
    response = HANDLERS[CrossQuestionnaireOptionDependencyError](
        _REQUEST, CrossQuestionnaireOptionDependencyError("bad option dependency")
    )
    assert response.status_code == 400
    assert json.loads(response.content) == {"detail": "bad option dependency"}


def test_disallowed_multiple_answers_error_maps_to_409() -> None:
    response = HANDLERS[DisallowedMultipleAnswersError](
        _REQUEST, DisallowedMultipleAnswersError("only one answer allowed")
    )
    assert response.status_code == 409
    assert json.loads(response.content) == {"detail": "only one answer allowed"}


def test_file_validation_error_maps_to_422() -> None:
    response = HANDLERS[FileValidationError](_REQUEST, FileValidationError("bad file"))
    assert response.status_code == 422
    assert json.loads(response.content) == {"detail": "bad file"}
