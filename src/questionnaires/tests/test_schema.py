"""test_schema.py: Unit tests for questionnaire schemas."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from questionnaires.models import (
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
)
from questionnaires.schema import (
    EvaluationCreateSchema,
    EvaluationResponseSchema,
    QuestionAnswerDetailSchema,
    SubmissionListItemSchema,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def evaluation_user() -> RevelUser:
    """Provides a user for evaluating submissions."""
    return RevelUser.objects.create_user(username="evaluator", email="evaluator@example.com", password="password")


def test_submission_list_item_schema_resolve_user_email(questionnaire: Questionnaire, user: RevelUser) -> None:
    """Test that SubmissionListItemSchema resolves user email correctly."""
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    user_email = SubmissionListItemSchema.resolve_user_email(submission)

    assert user_email == user.email


def test_submission_list_item_schema_resolve_user_name_with_preferred_name(questionnaire: Questionnaire) -> None:
    """Test that SubmissionListItemSchema resolves user name with preferred name."""
    user = RevelUser.objects.create_user(
        username="testuser", email="test@example.com", first_name="John", last_name="Doe", preferred_name="Johnny"
    )
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    user_name = SubmissionListItemSchema.resolve_user_name(submission)

    assert user_name == "Johnny"


def test_submission_list_item_schema_resolve_user_name_without_preferred_name(questionnaire: Questionnaire) -> None:
    """Test that SubmissionListItemSchema resolves user name from first/last name."""
    user = RevelUser.objects.create_user(
        username="testuser", email="test@example.com", first_name="John", last_name="Doe", preferred_name=""
    )
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    user_name = SubmissionListItemSchema.resolve_user_name(submission)

    assert user_name == "John Doe"


def test_submission_list_item_schema_resolve_questionnaire_name(questionnaire: Questionnaire, user: RevelUser) -> None:
    """Test that SubmissionListItemSchema resolves questionnaire name correctly."""
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    questionnaire_name = SubmissionListItemSchema.resolve_questionnaire_name(submission)

    assert questionnaire_name == questionnaire.name


def test_submission_list_item_schema_resolve_evaluation_status_with_evaluation(
    questionnaire: Questionnaire, user: RevelUser, evaluation_user: RevelUser
) -> None:
    """Test that SubmissionListItemSchema resolves evaluation status when evaluation exists."""
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.Status.APPROVED, evaluator=evaluation_user
    )
    submission.refresh_from_db()

    evaluation_status = SubmissionListItemSchema.resolve_evaluation_status(submission)

    assert evaluation_status == QuestionnaireEvaluation.Status.APPROVED


def test_submission_list_item_schema_resolve_evaluation_status_without_evaluation(
    questionnaire: Questionnaire, user: RevelUser
) -> None:
    """Test that SubmissionListItemSchema resolves evaluation status when no evaluation exists."""
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    evaluation_status = SubmissionListItemSchema.resolve_evaluation_status(submission)

    assert evaluation_status is None


def test_submission_list_item_schema_resolve_evaluation_score_with_evaluation(
    questionnaire: Questionnaire, user: RevelUser, evaluation_user: RevelUser
) -> None:
    """Test that SubmissionListItemSchema resolves evaluation score when evaluation exists."""
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)
    QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.Status.APPROVED,
        score=Decimal("87.50"),
        evaluator=evaluation_user,
    )
    submission.refresh_from_db()

    evaluation_score = SubmissionListItemSchema.resolve_evaluation_score(submission)

    assert evaluation_score == Decimal("87.50")


def test_submission_list_item_schema_resolve_evaluation_score_without_evaluation(
    questionnaire: Questionnaire, user: RevelUser
) -> None:
    """Test that SubmissionListItemSchema resolves evaluation score when no evaluation exists."""
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    evaluation_score = SubmissionListItemSchema.resolve_evaluation_score(submission)

    assert evaluation_score is None


def test_question_answer_detail_schema_multiple_choice() -> None:
    """Test that QuestionAnswerDetailSchema works for multiple choice questions."""
    import uuid

    option_id = uuid.uuid4()
    schema = QuestionAnswerDetailSchema(
        question_id=uuid.uuid4(),
        question_text="What is your favorite color?",
        question_type="multiple_choice",
        answer_content=[{"option_id": option_id, "option_text": "Blue", "is_correct": True}],
    )

    assert schema.question_type == "multiple_choice"
    assert isinstance(schema.answer_content, list)
    assert len(schema.answer_content) == 1
    assert schema.answer_content[0]["option_id"] == option_id
    assert schema.answer_content[0]["option_text"] == "Blue"
    assert schema.answer_content[0]["is_correct"] is True


def test_question_answer_detail_schema_free_text() -> None:
    """Test that QuestionAnswerDetailSchema works for free text questions."""
    import uuid

    schema = QuestionAnswerDetailSchema(
        question_id=uuid.uuid4(),
        question_text="Explain your reasoning.",
        question_type="free_text",
        answer_content=[{"answer": "This is my detailed explanation."}],
    )

    assert schema.question_type == "free_text"
    assert isinstance(schema.answer_content, list)
    assert len(schema.answer_content) == 1
    assert schema.answer_content[0]["answer"] == "This is my detailed explanation."
    assert "is_correct" not in schema.answer_content[0]


def test_evaluation_create_schema_valid() -> None:
    """Test that EvaluationCreateSchema validates correctly."""
    schema = EvaluationCreateSchema(
        status=QuestionnaireEvaluation.Status.APPROVED, score=Decimal("92.5"), comments="Excellent work!"
    )

    assert schema.status == QuestionnaireEvaluation.Status.APPROVED
    assert schema.score == Decimal("92.5")
    assert schema.comments == "Excellent work!"


def test_evaluation_create_schema_without_score() -> None:
    """Test that EvaluationCreateSchema works without score."""
    schema = EvaluationCreateSchema(
        status=QuestionnaireEvaluation.Status.REJECTED, comments="Needs improvement", score=None
    )

    assert schema.status == QuestionnaireEvaluation.Status.REJECTED
    assert schema.score is None
    assert schema.comments == "Needs improvement"


def test_evaluation_create_schema_score_validation() -> None:
    """Test that EvaluationCreateSchema validates score bounds."""
    import pytest
    from pydantic import ValidationError

    # Test score below minimum
    with pytest.raises(ValidationError):
        EvaluationCreateSchema(status=QuestionnaireEvaluation.Status.APPROVED, score=Decimal("-1.0"))

    # Test score above maximum
    with pytest.raises(ValidationError):
        EvaluationCreateSchema(status=QuestionnaireEvaluation.Status.APPROVED, score=Decimal("101.0"))

    # Test valid score at boundaries
    schema_min = EvaluationCreateSchema(status=QuestionnaireEvaluation.Status.APPROVED, score=Decimal("0.0"))
    assert schema_min.score == Decimal("0.0")

    schema_max = EvaluationCreateSchema(status=QuestionnaireEvaluation.Status.APPROVED, score=Decimal("100.0"))
    assert schema_max.score == Decimal("100.0")


def test_evaluation_response_schema_from_model(
    questionnaire: Questionnaire, user: RevelUser, evaluation_user: RevelUser
) -> None:
    """Test that EvaluationResponseSchema works with model data."""
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)
    evaluation = QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.Status.APPROVED,
        score=Decimal("88.0"),
        comments="Well done",
        evaluator=evaluation_user,
    )

    # Test that the schema can be created from the model
    schema = EvaluationResponseSchema.from_orm(evaluation)

    assert schema.id == evaluation.id
    assert schema.submission_id == submission.id
    assert schema.status == QuestionnaireEvaluation.Status.APPROVED
    assert schema.score == Decimal("88.0")
    assert schema.comments == "Well done"
    assert schema.evaluator_id == evaluation_user.id
    assert schema.created_at == evaluation.created_at
    assert schema.updated_at == evaluation.updated_at
