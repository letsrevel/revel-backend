"""Tests for QuestionnaireService.evaluate_submission() method."""

from decimal import Decimal

import pytest

from accounts.models import RevelUser
from questionnaires.models import (
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
)
from questionnaires.schema import EvaluationCreateSchema
from questionnaires.service import QuestionnaireService

pytestmark = pytest.mark.django_db


def test_evaluate_submission_create_new(
    questionnaire: Questionnaire, user: RevelUser, evaluator: RevelUser, org_questionnaire: object
) -> None:
    """Test creating a new evaluation for a submission."""
    # Create submission
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    # Create evaluator
    service = QuestionnaireService(questionnaire.id)
    payload = EvaluationCreateSchema(
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        score=Decimal("85.50"),
        comments="Good submission",
    )

    evaluation = service.evaluate_submission(submission.id, payload, evaluator)

    assert evaluation.submission == submission
    assert evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    assert evaluation.score == Decimal("85.50")
    assert evaluation.comments == "Good submission"
    assert evaluation.evaluator == evaluator
    assert evaluation.automatically_evaluated is False


def test_evaluate_submission_update_existing(
    questionnaire: Questionnaire, user: RevelUser, evaluator: RevelUser, org_questionnaire: object
) -> None:
    """Test updating an existing evaluation for a submission."""
    # Create submission and initial evaluation
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    initial_evaluation = QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW,
        score=Decimal("70.00"),
        comments="Initial evaluation",
        evaluator=evaluator,
    )

    service = QuestionnaireService(questionnaire.id)
    payload = EvaluationCreateSchema(
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        score=Decimal("60.00"),
        comments="Updated: needs improvement",
    )

    updated_evaluation = service.evaluate_submission(submission.id, payload, evaluator)

    # Should be the same evaluation object, just updated
    assert updated_evaluation.id == initial_evaluation.id
    assert updated_evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    assert updated_evaluation.score == Decimal("60.00")
    assert updated_evaluation.comments == "Updated: needs improvement"
    assert updated_evaluation.evaluator == evaluator
    assert updated_evaluation.automatically_evaluated is False


def test_evaluate_submission_wrong_questionnaire(
    questionnaire: Questionnaire, user: RevelUser, evaluator: RevelUser
) -> None:
    """Test that evaluate_submission raises error for wrong questionnaire."""
    # Create submission for different questionnaire
    other_questionnaire = Questionnaire.objects.create(name="Other Questionnaire")
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=other_questionnaire)

    service = QuestionnaireService(questionnaire.id)
    payload = EvaluationCreateSchema(
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED, score=None, comments="Should fail"
    )

    with pytest.raises(QuestionnaireSubmission.DoesNotExist):
        service.evaluate_submission(submission.id, payload, evaluator)
