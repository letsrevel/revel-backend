from uuid import UUID

from celery import shared_task

from .evaluator import SubmissionEvaluator
from .exceptions import SubmissionInDraftError
from .models import QuestionnaireSubmission


@shared_task
def evaluate_questionnaire_submission(questionnaire_submission_id: str) -> UUID:
    """Evaluate a questionnaire submission automatically."""
    submission = QuestionnaireSubmission.objects.get(id=questionnaire_submission_id)
    if submission.status == QuestionnaireSubmission.Status.DRAFT:
        raise SubmissionInDraftError("Submission is still in draft.")
    evaluator = SubmissionEvaluator(submission)
    result = evaluator.evaluate()
    return result.id
