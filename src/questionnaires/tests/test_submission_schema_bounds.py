"""Bounds and duplicate detection on questionnaire submissions."""

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


def test_total_answers_across_kinds_are_capped() -> None:
    mc = [{"question_id": str(uuid4()), "options_id": []} for _ in range(600)]
    ft = [{"question_id": str(uuid4()), "answer": "x"} for _ in range(600)]
    payload = {**_payload(mc), "free_text_answers": ft}
    with pytest.raises(ValidationError, match="at most 1000"):
        QuestionnaireSubmissionSchema.model_validate(payload)


def test_duplicate_question_ids_detected_on_large_list() -> None:
    qid = str(uuid4())
    dup = [{"question_id": qid, "options_id": []}, {"question_id": qid, "options_id": []}]
    with pytest.raises(ValidationError):
        QuestionnaireSubmissionSchema.model_validate(_payload(dup))
    many = [{"question_id": str(uuid4()), "options_id": []} for _ in range(1000)]
    QuestionnaireSubmissionSchema.model_validate(_payload(many))
