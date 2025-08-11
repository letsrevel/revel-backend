"""test_llm_backends.py: Tests for the LLM backend implementations."""

import uuid

from questionnaires.llms.llm_backends import MockEvaluator
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
