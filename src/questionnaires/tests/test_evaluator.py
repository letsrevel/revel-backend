"""test_evaluator.py: Unit tests for the SubmissionEvaluator service."""

from decimal import Decimal

import pytest

from questionnaires.evaluator import SubmissionEvaluator
from questionnaires.llms.llm_backends import MockEvaluator
from questionnaires.models import (
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
)


@pytest.mark.django_db
def test_evaluator_raises_error_for_draft_submission(
    draft_submission: QuestionnaireSubmission, mock_evaluator: MockEvaluator
) -> None:
    """Test that SubmissionEvaluator rejects a submission not in 'submitted' status."""
    with pytest.raises(ValueError, match="Only submitted questionnaires can be evaluated."):
        SubmissionEvaluator(submission=draft_submission, llm_evaluator=mock_evaluator)


@pytest.mark.django_db
def test_evaluation_automatic_mode_pass(
    submitted_submission: QuestionnaireSubmission,
    single_answer_mc_question: MultipleChoiceQuestion,
    correct_option: MultipleChoiceOption,
    free_text_question: FreeTextQuestion,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test a full evaluation in AUTOMATIC mode that results in a pass."""
    # Setup: 1 correct MC answer, 1 FT answer that will pass. Total 2/2 = 100%
    submitted_submission.questionnaire.evaluation_mode = Questionnaire.EvaluationMode.AUTOMATIC
    submitted_submission.questionnaire.min_score = 75
    submitted_submission.questionnaire.save()

    MultipleChoiceAnswer.objects.create(
        submission=submitted_submission, question=single_answer_mc_question, option=correct_option
    )
    FreeTextAnswer.objects.create(
        submission=submitted_submission, question=free_text_question, answer="This is a good response."
    )

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions
    assert evaluation.score == Decimal("100.00")
    assert evaluation.proposed_status == QuestionnaireEvaluation.ProposedStatus.APPROVED
    assert evaluation.status == QuestionnaireEvaluation.Status.APPROVED  # Automatic mode finalizes status
    assert evaluation.automatically_evaluated is True
    assert evaluation.raw_evaluation_data is not None
    assert evaluation.evaluation_data.mc_points_scored == Decimal("1.0")
    assert evaluation.evaluation_data.max_mc_points == Decimal("1.0")
    assert evaluation.evaluation_data.ft_points_scored == Decimal("1.0")
    assert evaluation.evaluation_data.max_ft_points == Decimal("1.0")


@pytest.mark.django_db
def test_evaluation_automatic_mode_fail(
    submitted_submission: QuestionnaireSubmission,
    single_answer_mc_question: MultipleChoiceQuestion,
    incorrect_option: MultipleChoiceOption,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test a full evaluation in AUTOMATIC mode that results in a fail."""
    # Setup: 1 incorrect MC answer. Total 0/1 = 0%
    submitted_submission.questionnaire.evaluation_mode = Questionnaire.EvaluationMode.AUTOMATIC
    submitted_submission.questionnaire.min_score = 75
    submitted_submission.questionnaire.save()

    MultipleChoiceAnswer.objects.create(
        submission=submitted_submission, question=single_answer_mc_question, option=incorrect_option
    )

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions
    assert evaluation.score == Decimal("0.0")
    assert evaluation.proposed_status == QuestionnaireEvaluation.ProposedStatus.REJECTED
    assert evaluation.status == QuestionnaireEvaluation.Status.REJECTED  # Automatic mode finalizes status


@pytest.mark.django_db
def test_evaluation_hybrid_mode(
    submitted_submission: QuestionnaireSubmission,
    free_text_question: FreeTextQuestion,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test evaluation in HYBRID mode, which should result in PENDING_REVIEW status."""
    # Setup: 1 FT answer that will fail. 0/1 = 0%
    submitted_submission.questionnaire.evaluation_mode = Questionnaire.EvaluationMode.HYBRID
    submitted_submission.questionnaire.min_score = 75
    submitted_submission.questionnaire.save()

    FreeTextAnswer.objects.create(
        submission=submitted_submission, question=free_text_question, answer="This is a bad response."
    )

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions
    assert evaluation.score == Decimal("0.00")
    assert evaluation.proposed_status == QuestionnaireEvaluation.ProposedStatus.REJECTED
    assert evaluation.status == QuestionnaireEvaluation.Status.PENDING_REVIEW  # Hybrid mode awaits human review


@pytest.mark.django_db
def test_evaluation_with_fatal_question_failure_overrides_high_score(
    submitted_submission: QuestionnaireSubmission,
    free_text_question: FreeTextQuestion,
    mock_evaluator: MockEvaluator,
) -> None:
    """
    Test that failing a fatal question results in REJECTED status,
    even if the score would have otherwise been a pass.
    """
    # Setup:
    # 1. A high-value question, answered correctly.
    # 2. A question with a penalty, answered incorrectly.
    # 3. A fatal question, answered incorrectly.
    # The score without the fatal rule would be (20 - 5) / (20 + 10 + 1) = 15/31 ~ 48%,
    # which we will set as a passing score to prove the fatal rule overrides it.
    questionnaire = submitted_submission.questionnaire
    questionnaire.min_score = 40  # Set a passing score that would be met without the fatal rule
    questionnaire.save()

    # Question 1: High value, correct answer
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="High value", positive_weight=Decimal("20.0")
    )
    q1_correct_option = MultipleChoiceOption.objects.create(question=q1, option="Correct", is_correct=True)
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q1, option=q1_correct_option)

    # Question 2: Penalty question, incorrect answer
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Penalty", positive_weight=Decimal("10.0"), negative_weight=Decimal("5.0")
    )
    q2_incorrect_option = MultipleChoiceOption.objects.create(question=q2, option="Incorrect", is_correct=False)
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q2, option=q2_incorrect_option)

    # Question 3: Fatal question, incorrect answer
    q3 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Fatal", positive_weight=Decimal("1.0"), is_fatal=True
    )
    q3_incorrect_option = MultipleChoiceOption.objects.create(question=q3, option="Incorrect", is_correct=False)
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q3, option=q3_incorrect_option)

    FreeTextAnswer.objects.create(
        submission=submitted_submission, question=free_text_question, answer="This is a good response."
    )  # This won't be evaluated, because we have a fatal error, so the score won't be affected

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions
    # The fatal rule should override everything else.
    assert evaluation.proposed_status == QuestionnaireEvaluation.ProposedStatus.REJECTED
    assert evaluation.status == QuestionnaireEvaluation.Status.REJECTED  # Assuming AUTOMATIC mode
    assert evaluation.score == Decimal("-100.0")  # Final score is set to -100 on fatal failure

    # Also check the audit data to ensure points were calculated correctly before the override
    assert evaluation.evaluation_data.max_mc_points == Decimal("31.0")  # 20 + 10 + 1
    assert evaluation.evaluation_data.mc_points_scored == Decimal("15.0")  # 20 (from q1) - 5 (from q2 penalty)
    assert evaluation.evaluation_data.max_ft_points == Decimal("1.0")
    assert evaluation.evaluation_data.ft_points_scored == Decimal("0.0")


@pytest.mark.django_db
def test_evaluation_with_no_answers(
    submitted_submission: QuestionnaireSubmission, mock_evaluator: MockEvaluator
) -> None:
    """Test evaluation of a submission with no answers, which should pass with 100%."""
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()
    assert evaluation.score == Decimal("100.00")
    assert evaluation.proposed_status == QuestionnaireEvaluation.ProposedStatus.APPROVED
    assert evaluation.raw_evaluation_data is not None
    assert evaluation.evaluation_data.max_mc_points == Decimal("0.0")
    assert evaluation.evaluation_data.max_ft_points == Decimal("0.0")


@pytest.mark.django_db
def test_evaluation_with_no_ft_answers(
    submitted_submission: QuestionnaireSubmission,
    single_answer_mc_question: MultipleChoiceQuestion,
    correct_option: MultipleChoiceOption,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test evaluation of a submission that only has MC answers."""
    MultipleChoiceAnswer.objects.create(
        submission=submitted_submission, question=single_answer_mc_question, option=correct_option
    )
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()
    assert evaluation.score == Decimal("100.00")
    assert evaluation.raw_evaluation_data is not None
    assert evaluation.evaluation_data.max_mc_points == Decimal("1.0")
    assert evaluation.evaluation_data.ft_points_scored == Decimal("0.0")


@pytest.mark.django_db
def test_evaluation_updates_existing_record(
    submitted_submission: QuestionnaireSubmission, mock_evaluator: MockEvaluator
) -> None:
    """Test that calling evaluate() twice on the same submission updates the existing evaluation."""
    # First evaluation
    evaluator1 = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation1 = evaluator1.evaluate()
    assert QuestionnaireEvaluation.objects.count() == 1

    # Second evaluation (e.g., re-run with a different LLM or after a code change)
    evaluator2 = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation2 = evaluator2.evaluate()

    assert QuestionnaireEvaluation.objects.count() == 1
    assert evaluation1.pk == evaluation2.pk


@pytest.mark.django_db
def test_evaluation_fails_if_mandatory_question_is_unanswered(
    submitted_submission: QuestionnaireSubmission,
    single_answer_mc_question: MultipleChoiceQuestion,  # This will be the mandatory but unanswered question
    mock_evaluator: MockEvaluator,
) -> None:
    """Test that the evaluation is REJECTED if a mandatory question is not answered."""
    # Setup: Mark a question as mandatory but do not provide an answer for it.
    single_answer_mc_question.is_mandatory = True
    single_answer_mc_question.save()

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions
    assert evaluation.score == Decimal("-100.0")
    assert evaluation.proposed_status == QuestionnaireEvaluation.ProposedStatus.REJECTED
    assert evaluation.status == QuestionnaireEvaluation.Status.REJECTED  # Assuming AUTOMATIC mode
    assert evaluation.evaluation_data.missing_mandatory == [single_answer_mc_question.id]
