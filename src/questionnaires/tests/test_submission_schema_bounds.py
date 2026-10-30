"""Bounds and duplicate detection on questionnaire submissions."""

import time
import typing as t
from uuid import uuid4

import pytest
from pydantic import ValidationError

from questionnaires.schema import QuestionnaireSubmissionSchema


def _payload(answers: list[dict[str, t.Any]]) -> dict[str, object]:
    return {"questionnaire_id": str(uuid4()), "multiple_choice_answers": answers, "status": "ready"}


def test_answer_lists_are_capped() -> None:
    answers = [{"question_id": str(uuid4()), "options_id": []} for _ in range(1001)]
    with pytest.raises(ValidationError):
        QuestionnaireSubmissionSchema.model_validate(_payload(answers))


def test_duplicate_question_ids_detected_in_linear_time() -> None:
    qid = str(uuid4())
    dup = [{"question_id": qid, "options_id": []}, {"question_id": qid, "options_id": []}]
    with pytest.raises(ValidationError):
        QuestionnaireSubmissionSchema.model_validate(_payload(dup))
    many = [{"question_id": str(uuid4()), "options_id": []} for _ in range(1000)]
    start = time.monotonic()
    QuestionnaireSubmissionSchema.model_validate(_payload(many))
    assert time.monotonic() - start < 2
