"""test_llm_backends.py: Tests for the LLM backend implementations."""

import typing as t
import uuid

import pytest

from questionnaires.llms.llm_backends import MockEvaluator, _strip_tags_and_content
from questionnaires.llms.llm_interfaces import AnswerToEvaluate


def test_mock_evaluator_passing_case() -> None:
    """Test that the mock evaluator passes an answer containing 'good'."""
    evaluator = MockEvaluator()
    q_id = uuid.uuid4()
    questions = [
        AnswerToEvaluate(question_id=q_id, question_text="Q1", answer_text="This is a good answer.", guidelines="")
    ]

    response = evaluator.evaluate(questions_to_evaluate=questions, questionnaire_guidelines=None)

    assert len(response.evaluations) == 1
    assert response.evaluations[0].is_passing is True
    assert response.evaluations[0].explanation == "The answer contained the keyword 'good'."
    assert response.evaluations[0].question_id == q_id


def test_mock_evaluator_failing_case() -> None:
    """Test that the mock evaluator fails an answer not containing 'good'."""
    evaluator = MockEvaluator()
    q_id = uuid.uuid4()
    questions = [
        AnswerToEvaluate(question_id=q_id, question_text="Q2", answer_text="This is a bad answer.", guidelines="")
    ]

    response = evaluator.evaluate(questions_to_evaluate=questions, questionnaire_guidelines=None)

    assert len(response.evaluations) == 1
    assert response.evaluations[0].is_passing is False
    assert response.evaluations[0].explanation == "The answer did not contain the keyword 'good'."
    assert response.evaluations[0].question_id == q_id


def test_mock_evaluator_batch_case() -> None:
    """Test that the mock evaluator handles a batch of mixed questions."""
    evaluator = MockEvaluator()
    q_id_1 = uuid.uuid4()
    q_id_2 = uuid.uuid4()
    questions = [
        AnswerToEvaluate(question_id=q_id_1, question_text="Q1", answer_text="This is a good answer.", guidelines=""),
        AnswerToEvaluate(question_id=q_id_2, question_text="Q2", answer_text="This is a bad answer.", guidelines=""),
    ]

    response = evaluator.evaluate(questions_to_evaluate=questions, questionnaire_guidelines=None)

    assert len(response.evaluations) == 2
    assert response.evaluations[0].is_passing is True
    assert response.evaluations[1].is_passing is False


def test_mock_evaluator_empty_batch() -> None:
    """Test that the mock evaluator handles an empty list of questions."""
    evaluator = MockEvaluator()
    response = evaluator.evaluate(questions_to_evaluate=[], questionnaire_guidelines=None)
    assert len(response.evaluations) == 0


# Tag stripper

CASES_SIMPLE: t.Final[list[tuple[str, str]]] = [
    # no tags
    ("plain text only", "plain text only"),
    # paired tags -> remove whole block
    ("<x>foo</x> bar", "bar"),
    ("keep <tag>nuke me</tag> safe", "keep safe"),
    # nested -> outer should be removed entirely
    ("a <t>one <t>two</t> three</t> b", "a two b"),
    # multiple peers -> both removed
    ("<t>one</t><t>two</t>", ""),
    # weird names (with whitespace)
    ("x <this is cursed>boom</this is cursed> y", "x y"),
    # attributes and mixed spacing -> content inside removed
    ("<tag a=1 b=2> X </tag> Y", "Y"),
    # whitespace normalization around removals
    ("  <a> x </a>   keep   <b/>   also  ", "keep <b/> also"),
    # angle-brackets for math are NOT tags; do not change
    ("Price < 100 and > 50", "Price < 100 and > 50"),
    # empty content
    ("<x></x>stay", "stay"),
]


def _nest(tag: str, payload: str, depth: int) -> str:
    open_tag = f"<{tag}>"
    close_tag = f"</{tag}>"
    return f"{open_tag * depth}{payload}{close_tag * depth}"


DEEP_NESTED_INPUT: t.Final[str] = f"keep {_nest('t', _nest('u', 'spam', 10), 10)} end"
DEEP_NESTED_EXPECTED: t.Final[str] = "keep spam end"


@pytest.mark.parametrize(("raw", "expected"), CASES_SIMPLE)
def test_strip_tags_and_content_basic(raw: str, expected: str) -> None:
    actual: str = _strip_tags_and_content(raw)
    assert actual == expected


def test_strip_tags_and_content_deep_nesting() -> None:
    actual: str = _strip_tags_and_content(DEEP_NESTED_INPUT)
    assert actual == DEEP_NESTED_EXPECTED


@pytest.mark.parametrize(
    ("raw",),
    [
        ("plain text only",),
        ("before <t/> after",),
        ("<x>foo</x> bar",),
        (DEEP_NESTED_INPUT,),
        ("Price < 100 and > 50",),  # non-tag math should remain stable
    ],
)
def test_strip_tags_and_content_idempotent(raw: str) -> None:
    once: str = _strip_tags_and_content(raw)
    twice: str = _strip_tags_and_content(once)
    assert twice == once


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a   b\tc\n\n<z>kill</z>   d", "a b c d"),
        ("   <t>kill</t>   kept   ", "kept"),
        ("\n<x>\nzzz\n</x>\nY\n", "Y"),
    ],
)
def test_strip_tags_and_content_whitespace_normalization(raw: str, expected: str) -> None:
    actual: str = _strip_tags_and_content(raw)
    assert actual == expected
