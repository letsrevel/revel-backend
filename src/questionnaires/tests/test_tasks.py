import pytest

from questionnaires.exceptions import SubmissionInDraftError
from questionnaires.models import QuestionnaireEvaluation, QuestionnaireSubmission
from questionnaires.tasks import evaluate_questionnaire_submission


@pytest.mark.django_db
def test_evaluate_questionnaire_submission_task_success(
    submitted_submission: QuestionnaireSubmission,
) -> None:
    """Test that the task correctly evaluates a submitted questionnaire."""
    evaluate_questionnaire_submission(str(submitted_submission.pk))
    assert QuestionnaireEvaluation.objects.filter(submission=submitted_submission).exists()


@pytest.mark.django_db
def test_evaluate_questionnaire_submission_task_draft_error(
    draft_submission: QuestionnaireSubmission,
) -> None:
    """Test that the task raises an error for a draft submission."""
    with pytest.raises(SubmissionInDraftError):
        evaluate_questionnaire_submission(str(draft_submission.pk))
