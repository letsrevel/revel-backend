"""Tests for file upload question evaluation in SubmissionEvaluator.

This module tests:
- Missing mandatory file upload questions fail evaluation
- Conditional file upload questions respect dependencies
- File upload questions don't contribute to score (informational only)
- Applicable file upload questions tracking
"""

from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import RevelUser
from conftest import RevelUserFactory
from questionnaires.evaluator import SubmissionEvaluator
from questionnaires.llms.llm_backends import MockEvaluator
from questionnaires.models import (
    FileUploadAnswer,
    FileUploadQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireFile,
    QuestionnaireSection,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# --- Helper fixtures ---


@pytest.fixture
def user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Provides a standard user instance."""
    return revel_user_factory()


@pytest.fixture
def mock_evaluator() -> MockEvaluator:
    """Provides an instance of the MockEvaluator."""
    return MockEvaluator()


@pytest.fixture
def submitted_submission(user: RevelUser, questionnaire: Questionnaire) -> QuestionnaireSubmission:
    """Provides a submitted submission, ready for evaluation."""
    return QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )


@pytest.fixture
def questionnaire_file(user: RevelUser) -> QuestionnaireFile:
    """Creates a QuestionnaireFile for testing."""
    uploaded_file = SimpleUploadedFile(
        name="test_file.pdf",
        content=b"test content",
        content_type="application/pdf",
    )
    return QuestionnaireFile.objects.create(
        uploader=user,
        file=uploaded_file,
        original_filename="test_file.pdf",
        file_hash="eval_test_hash",
        mime_type="application/pdf",
        file_size=12,
    )


# --- Tests for missing mandatory file upload questions ---


class TestMissingMandatoryFileUploadQuestions:
    """Tests for evaluation failing when mandatory file upload questions are missing."""

    def test_evaluation_fails_if_mandatory_file_upload_unanswered(
        self,
        submitted_submission: QuestionnaireSubmission,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test that evaluation is REJECTED if a mandatory file upload question is not answered."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Required document upload",
            is_mandatory=True,
        )
        # No FileUploadAnswer created - missing mandatory answer

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert
        assert evaluation.score == Decimal("-100.0")
        assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.REJECTED
        assert evaluation.evaluation_data.missing_mandatory == [fu_question.id]

    def test_evaluation_passes_if_mandatory_file_upload_answered(
        self,
        user: RevelUser,
        submitted_submission: QuestionnaireSubmission,
        questionnaire_file: QuestionnaireFile,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test that evaluation passes when mandatory file upload question is answered."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Required document upload",
            is_mandatory=True,
        )
        fu_answer = FileUploadAnswer.objects.create(
            submission=submitted_submission,
            question=fu_question,
        )
        fu_answer.files.add(questionnaire_file)

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert
        assert evaluation.score == Decimal("100.00")
        assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED
        assert not evaluation.evaluation_data.missing_mandatory

    def test_evaluation_fails_with_mix_of_missing_mandatory_types(
        self,
        user: RevelUser,
        submitted_submission: QuestionnaireSubmission,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test evaluation fails when any mandatory question type is missing."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        mc_question = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="Mandatory MC",
            is_mandatory=True,
            order=1,
        )
        MultipleChoiceOption.objects.create(
            question=mc_question,
            option="Option A",
            is_correct=True,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Mandatory FU",
            is_mandatory=True,
            order=2,
        )
        # Only answer MC question, not FU question
        mc_option = mc_question.options.first()
        assert mc_option is not None
        MultipleChoiceAnswer.objects.create(
            submission=submitted_submission,
            question=mc_question,
            option=mc_option,
        )

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert
        assert evaluation.score == Decimal("-100.0")
        assert fu_question.id in evaluation.evaluation_data.missing_mandatory  # type: ignore[operator]


# --- Tests for conditional file upload questions ---


class TestConditionalFileUploadQuestions:
    """Tests for conditional file upload questions respecting dependencies."""

    def test_conditional_fu_question_not_applicable_when_option_not_selected(
        self,
        submitted_submission: QuestionnaireSubmission,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test that conditional FU question is not applicable when trigger option not selected."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
        questionnaire.save()

        # Q1: "Do you have documents?" with Yes/No options
        q1 = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="Do you have documents?",
            order=1,
            allow_multiple_answers=True,
        )
        q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True)
        q1_no = MultipleChoiceOption.objects.create(question=q1, option="No", is_correct=True)

        # Q2: Conditional FU question that depends on Q1=Yes, mandatory
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload your documents",
            order=2,
            is_mandatory=True,
            depends_on_option=q1_yes,
        )

        # User answers Q1=No (so FU question should NOT be applicable)
        MultipleChoiceAnswer.objects.create(
            submission=submitted_submission,
            question=q1,
            option=q1_no,
        )
        # User does NOT answer FU question (which is mandatory but should be skipped)

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert - FU question not applicable, so missing mandatory should be empty
        missing = evaluation.evaluation_data.missing_mandatory
        assert missing is None or missing == []
        assert evaluation.score == Decimal("100.00")
        assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED

    def test_conditional_fu_question_applicable_when_option_selected(
        self,
        submitted_submission: QuestionnaireSubmission,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test that conditional mandatory FU question fails submission when applicable but not answered."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
        questionnaire.save()

        # Q1: "Do you have documents?" with Yes option
        q1 = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="Do you have documents?",
            order=1,
        )
        q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True)

        # Q2: Conditional FU question that depends on Q1=Yes, mandatory
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload your documents",
            order=2,
            is_mandatory=True,
            depends_on_option=q1_yes,
        )

        # User answers Q1=Yes (so FU question SHOULD be applicable)
        MultipleChoiceAnswer.objects.create(
            submission=submitted_submission,
            question=q1,
            option=q1_yes,
        )
        # User does NOT answer FU question (which is mandatory and now applicable)

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert - FU question is applicable and mandatory but not answered - should fail
        assert evaluation.evaluation_data.missing_mandatory == [fu_question.id]
        assert evaluation.score == Decimal("-100.0")
        assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.REJECTED

    def test_conditional_section_makes_fu_questions_not_applicable(
        self,
        submitted_submission: QuestionnaireSubmission,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test that FU questions in a conditional section are not applicable when section condition not met."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
        questionnaire.save()

        # Q1: "Do you want to upload documents?" with Yes/No options
        q1 = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="Do you want to upload documents?",
            order=1,
            allow_multiple_answers=True,
        )
        q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True)
        q1_no = MultipleChoiceOption.objects.create(question=q1, option="No", is_correct=True)

        # Section that depends on Q1=Yes
        section = QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="Upload Section",
            order=1,
            depends_on_option=q1_yes,
        )

        # FU question inside the conditional section
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Upload your documents",
            order=1,
            is_mandatory=True,
        )

        # User answers Q1=No (so the section and FU question should NOT be applicable)
        MultipleChoiceAnswer.objects.create(
            submission=submitted_submission,
            question=q1,
            option=q1_no,
        )
        # User does NOT answer FU question

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert - FU question is in a non-applicable section, so missing mandatory should be empty
        missing = evaluation.evaluation_data.missing_mandatory
        assert missing is None or missing == []
        assert evaluation.score == Decimal("100.00")
        assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED

    def test_conditional_section_fu_question_answered_when_applicable(
        self,
        user: RevelUser,
        submitted_submission: QuestionnaireSubmission,
        questionnaire_file: QuestionnaireFile,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test that FU questions in a conditional section pass when section is applicable and answered."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
        questionnaire.save()

        # Q1: "Do you want to upload documents?"
        q1 = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="Do you want to upload documents?",
            order=1,
        )
        q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True)

        # Section that depends on Q1=Yes
        section = QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="Upload Section",
            order=1,
            depends_on_option=q1_yes,
        )

        # FU question inside the conditional section
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Upload your documents",
            order=1,
            is_mandatory=True,
        )

        # User answers Q1=Yes (so the section and FU question ARE applicable)
        MultipleChoiceAnswer.objects.create(
            submission=submitted_submission,
            question=q1,
            option=q1_yes,
        )
        # User answers FU question
        fu_answer = FileUploadAnswer.objects.create(
            submission=submitted_submission,
            question=fu_question,
        )
        fu_answer.files.add(questionnaire_file)

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert
        assert not evaluation.evaluation_data.missing_mandatory
        assert evaluation.score == Decimal("100.00")
        assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED


# --- Tests for file upload questions being informational (no scoring) ---


class TestFileUploadQuestionsAreInformational:
    """Tests that file upload questions don't contribute to scoring."""

    def test_fu_question_does_not_affect_max_points(
        self,
        user: RevelUser,
        submitted_submission: QuestionnaireSubmission,
        questionnaire_file: QuestionnaireFile,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test that file upload questions don't add to max points calculation."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
        questionnaire.save()

        # MC question worth 2 points
        mc_question = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="MC Question",
            positive_weight=Decimal("2.0"),
            order=1,
        )
        mc_correct = MultipleChoiceOption.objects.create(
            question=mc_question,
            option="Correct",
            is_correct=True,
        )

        # FU question - informational, even if weight is set it shouldn't count
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload document",
            positive_weight=Decimal("3.0"),  # This should be ignored
            order=2,
        )

        # Answer both questions correctly
        MultipleChoiceAnswer.objects.create(
            submission=submitted_submission,
            question=mc_question,
            option=mc_correct,
        )
        fu_answer = FileUploadAnswer.objects.create(
            submission=submitted_submission,
            question=fu_question,
        )
        fu_answer.files.add(questionnaire_file)

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert - Only MC points should be counted
        assert evaluation.evaluation_data.max_mc_points == Decimal("2.0")
        assert evaluation.evaluation_data.mc_points_scored == Decimal("2.0")
        # FU questions are informational - no fu_points fields exist yet
        assert evaluation.score == Decimal("100.00")

    def test_fu_question_missing_does_not_affect_score_when_not_mandatory(
        self,
        submitted_submission: QuestionnaireSubmission,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test that non-mandatory FU questions don't affect score when not answered."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
        questionnaire.save()

        # MC question worth 1 point
        mc_question = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="MC Question",
            positive_weight=Decimal("1.0"),
            order=1,
        )
        mc_correct = MultipleChoiceOption.objects.create(
            question=mc_question,
            option="Correct",
            is_correct=True,
        )

        # Non-mandatory FU question
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Optional upload",
            is_mandatory=False,
            order=2,
        )

        # Only answer MC question
        MultipleChoiceAnswer.objects.create(
            submission=submitted_submission,
            question=mc_question,
            option=mc_correct,
        )
        # Don't answer FU question

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert - Should pass with 100% (only MC counts)
        assert evaluation.score == Decimal("100.00")
        assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED

    def test_evaluation_with_only_fu_questions_and_no_answers(
        self,
        submitted_submission: QuestionnaireSubmission,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test evaluation with only non-mandatory FU questions and no answers passes."""
        # Arrange
        questionnaire = submitted_submission.questionnaire
        questionnaire.evaluation_mode = Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC
        questionnaire.save()

        # Only non-mandatory FU questions
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Optional upload 1",
            is_mandatory=False,
        )
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Optional upload 2",
            is_mandatory=False,
        )
        # No answers

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )
        evaluation = evaluator.evaluate()

        # Assert - No scorable questions, should pass with 100%
        assert evaluation.score == Decimal("100.00")
        assert evaluation.proposed_status == QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED


# --- Tests for applicable FU question tracking ---


class TestApplicableFileUploadQuestionTracking:
    """Tests for _applicable_fuq_ids tracking in evaluator."""

    def test_applicable_fu_questions_tracked_correctly(
        self,
        submitted_submission: QuestionnaireSubmission,
        mock_evaluator: MockEvaluator,
    ) -> None:
        """Test that applicable file upload questions are tracked correctly."""
        # Arrange
        questionnaire = submitted_submission.questionnaire

        # Q1: Base question
        q1 = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="Base question",
            order=1,
            allow_multiple_answers=True,
        )
        q1_a = MultipleChoiceOption.objects.create(question=q1, option="A", is_correct=True)
        q1_b = MultipleChoiceOption.objects.create(question=q1, option="B", is_correct=True)

        # FU question conditional on Q1=A
        fu_a = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="FU for A",
            order=2,
            depends_on_option=q1_a,
        )

        # FU question conditional on Q1=B
        fu_b = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="FU for B",
            order=3,
            depends_on_option=q1_b,
        )

        # Unconditional FU question
        fu_always = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="FU always",
            order=4,
        )

        # User answers Q1=A only
        MultipleChoiceAnswer.objects.create(
            submission=submitted_submission,
            question=q1,
            option=q1_a,
        )

        # Act
        evaluator = SubmissionEvaluator(
            submission=submitted_submission,
            llm_evaluator=mock_evaluator,
        )

        # Assert - Check internal tracking
        assert fu_a.id in evaluator._applicable_fuq_ids
        assert fu_b.id not in evaluator._applicable_fuq_ids  # Q1=B not selected
        assert fu_always.id in evaluator._applicable_fuq_ids  # Always applicable
