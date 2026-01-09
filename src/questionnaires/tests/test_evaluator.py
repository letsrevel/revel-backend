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
    QuestionnaireSection,
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
    submitted_submission.questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
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
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED
    assert (
        evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    )  # Automatic mode finalizes status
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
    submitted_submission.questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
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
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.REJECTED
    assert (
        evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    )  # Automatic mode finalizes status


@pytest.mark.django_db
def test_evaluation_hybrid_mode(
    submitted_submission: QuestionnaireSubmission,
    free_text_question: FreeTextQuestion,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test evaluation in HYBRID mode, which should result in PENDING_REVIEW status."""
    # Setup: 1 FT answer that will fail. 0/1 = 0%
    submitted_submission.questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.HYBRID
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
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.REJECTED
    assert (
        evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW
    )  # Hybrid mode awaits human review


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
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.REJECTED
    assert (
        evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    )  # Assuming AUTOMATIC mode
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
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED
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
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.REJECTED
    assert (
        evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    )  # Assuming AUTOMATIC mode
    assert evaluation.evaluation_data.missing_mandatory == [single_answer_mc_question.id]


# --- Conditional Questions Tests ---


@pytest.mark.django_db
def test_conditional_question_not_applicable_when_option_not_selected(
    submitted_submission: QuestionnaireSubmission,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test that a conditional question is not applicable when its depends_on_option is not selected."""
    questionnaire = submitted_submission.questionnaire
    questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
    questionnaire.save()

    # Q1: "Do you have allergies?" with Yes/No options (both are valid answers)
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you have allergies?", order=1, allow_multiple_answers=True
    )
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True)
    q1_no = MultipleChoiceOption.objects.create(question=q1, option="No", is_correct=True)

    # Q2: Conditional question that depends on Q1=Yes, mandatory
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        question="Are any life-threatening?",
        order=2,
        is_mandatory=True,
        depends_on_option=q1_yes,
        allow_multiple_answers=True,
    )
    MultipleChoiceOption.objects.create(question=q2, option="Yes", is_correct=True)
    MultipleChoiceOption.objects.create(question=q2, option="No", is_correct=True)

    # User answers Q1=No (so Q2 should NOT be applicable)
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q1, option=q1_no)
    # User does NOT answer Q2 (which is mandatory but should be skipped)

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions: Q2 is not applicable, so missing mandatory should be empty
    assert evaluation.evaluation_data.missing_mandatory is None or evaluation.evaluation_data.missing_mandatory == []
    assert evaluation.score == Decimal("100.00")  # 1/1 questions answered correctly
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED


@pytest.mark.django_db
def test_conditional_question_applicable_when_option_selected(
    submitted_submission: QuestionnaireSubmission,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test that a conditional mandatory question fails submission when applicable but not answered."""
    questionnaire = submitted_submission.questionnaire
    questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
    questionnaire.save()

    # Q1: "Do you have allergies?" with Yes/No options
    q1 = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Do you have allergies?", order=1)
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True)

    # Q2: Conditional question that depends on Q1=Yes, mandatory
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        question="Are any life-threatening?",
        order=2,
        is_mandatory=True,
        depends_on_option=q1_yes,
    )
    MultipleChoiceOption.objects.create(question=q2, option="Yes", is_correct=True)

    # User answers Q1=Yes (so Q2 SHOULD be applicable)
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q1, option=q1_yes)
    # User does NOT answer Q2 (which is mandatory and now applicable)

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions: Q2 is applicable and mandatory but not answered - should fail
    assert evaluation.evaluation_data.missing_mandatory == [q2.id]
    assert evaluation.score == Decimal("-100.0")
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.REJECTED


@pytest.mark.django_db
def test_conditional_section_makes_questions_not_applicable(
    submitted_submission: QuestionnaireSubmission,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test that questions in a conditional section are not applicable when section condition not met."""
    questionnaire = submitted_submission.questionnaire
    questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
    questionnaire.save()

    # Q1: "Do you want details?" with Yes/No options (both are valid answers)
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you want details?", order=1, allow_multiple_answers=True
    )
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True)
    q1_no = MultipleChoiceOption.objects.create(question=q1, option="No", is_correct=True)

    # Section that depends on Q1=Yes
    section = QuestionnaireSection.objects.create(
        questionnaire=questionnaire, name="Details Section", order=1, depends_on_option=q1_yes
    )

    # Q2: Mandatory question inside the conditional section
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        section=section,
        question="Please provide details",
        order=2,
        is_mandatory=True,
    )
    MultipleChoiceOption.objects.create(question=q2, option="Detail A", is_correct=True)

    # User answers Q1=No (so the section and Q2 should NOT be applicable)
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q1, option=q1_no)
    # User does NOT answer Q2

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions: Q2 is in a non-applicable section, so missing mandatory should be empty
    assert evaluation.evaluation_data.missing_mandatory is None or evaluation.evaluation_data.missing_mandatory == []
    assert evaluation.score == Decimal("100.00")
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED


@pytest.mark.django_db
def test_conditional_section_questions_scored_when_applicable(
    submitted_submission: QuestionnaireSubmission,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test that questions in a conditional section are scored when the section is applicable."""
    questionnaire = submitted_submission.questionnaire
    questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
    questionnaire.save()

    # Q1: "Do you want details?" with Yes/No options
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you want details?", order=1, positive_weight=Decimal("1.0")
    )
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True)

    # Section that depends on Q1=Yes
    section = QuestionnaireSection.objects.create(
        questionnaire=questionnaire, name="Details Section", order=1, depends_on_option=q1_yes
    )

    # Q2: Question inside the conditional section
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        section=section,
        question="Please provide details",
        order=2,
        positive_weight=Decimal("2.0"),
    )
    q2_correct = MultipleChoiceOption.objects.create(question=q2, option="Detail A", is_correct=True)

    # User answers Q1=Yes (so the section and Q2 ARE applicable)
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q1, option=q1_yes)
    # User answers Q2 correctly
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q2, option=q2_correct)

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions: Both questions are scored
    assert evaluation.evaluation_data.max_mc_points == Decimal("3.0")  # 1.0 + 2.0
    assert evaluation.evaluation_data.mc_points_scored == Decimal("3.0")
    assert evaluation.score == Decimal("100.00")
    assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED


@pytest.mark.django_db
def test_non_applicable_questions_not_counted_in_max_points(
    submitted_submission: QuestionnaireSubmission,
    mock_evaluator: MockEvaluator,
) -> None:
    """Test that non-applicable questions are not counted in max points calculation."""
    questionnaire = submitted_submission.questionnaire
    questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
    questionnaire.save()

    # Q1: Base question (allow multiple so both options can be "correct")
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        question="Pick one",
        order=1,
        positive_weight=Decimal("1.0"),
        allow_multiple_answers=True,
    )
    q1_a = MultipleChoiceOption.objects.create(question=q1, option="A", is_correct=True)
    q1_b = MultipleChoiceOption.objects.create(question=q1, option="B", is_correct=True)

    # Q2: Conditional on Q1=A, worth 3 points
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        question="Follow-up for A",
        order=2,
        positive_weight=Decimal("3.0"),
        depends_on_option=q1_a,
    )
    q2_correct = MultipleChoiceOption.objects.create(question=q2, option="X", is_correct=True)

    # Q3: Conditional on Q1=B, worth 2 points
    q3 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        question="Follow-up for B",
        order=3,
        positive_weight=Decimal("2.0"),
        depends_on_option=q1_b,
    )
    MultipleChoiceOption.objects.create(question=q3, option="Y", is_correct=True)

    # User answers Q1=A and Q2 correctly (so Q2 is applicable, Q3 is not)
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q1, option=q1_a)
    MultipleChoiceAnswer.objects.create(submission=submitted_submission, question=q2, option=q2_correct)

    # Action
    evaluator = SubmissionEvaluator(submission=submitted_submission, llm_evaluator=mock_evaluator)
    evaluation = evaluator.evaluate()

    # Assertions: Only Q1 and Q2 should be counted (Q3 is not applicable)
    # Max points should be 1.0 + 3.0 = 4.0 (not 1.0 + 3.0 + 2.0 = 6.0)
    assert evaluation.evaluation_data.max_mc_points == Decimal("4.0")
    assert evaluation.evaluation_data.mc_points_scored == Decimal("4.0")  # Q1 + Q2 answered correctly
    assert evaluation.score == Decimal("100.00")
