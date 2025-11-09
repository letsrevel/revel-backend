from uuid import UUID

import structlog
from celery import shared_task

from .evaluator import SubmissionEvaluator
from .exceptions import SubmissionInDraftError
from .models import QuestionnaireSubmission

logger = structlog.get_logger(__name__)


@shared_task
def evaluate_questionnaire_submission(questionnaire_submission_id: str) -> UUID:
    """Evaluate a questionnaire submission automatically."""
    logger.info("questionnaire_evaluation_task_started", submission_id=questionnaire_submission_id)
    submission = QuestionnaireSubmission.objects.get(id=questionnaire_submission_id)
    if submission.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT:
        logger.warning("questionnaire_evaluation_task_draft_error", submission_id=questionnaire_submission_id)
        raise SubmissionInDraftError("Submission is still in draft.")
    try:
        evaluator = SubmissionEvaluator(submission)
        result = evaluator.evaluate()
        logger.info(
            "questionnaire_evaluation_task_completed",
            submission_id=questionnaire_submission_id,
            evaluation_id=str(result.id),
        )
        return result.id
    except Exception as e:
        logger.error(
            "questionnaire_evaluation_task_failed",
            submission_id=questionnaire_submission_id,
            error=str(e),
            exc_info=True,
        )
        raise
