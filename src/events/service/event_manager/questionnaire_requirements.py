"""Evaluation of admission questionnaires against a user's submissions.

Shared by the event eligibility gate and the series pass purchase check, so both
apply the same missing / pending review / failed verdicts.
"""

import typing as t
import uuid

from django.utils.translation import gettext as _

from events.models import OrganizationQuestionnaire
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission
from questionnaires.utils.retake_policy import RetakeVerdict, approval_is_stale, evaluate_retake_policy

from .enums import NextStep, Reasons
from .types import EventUserEligibility


class QuestionnaireRequirementCheck:
    """Evaluate admission questionnaires against a user's submissions: missing, then pending review, then failed."""

    def __init__(
        self,
        event_id: uuid.UUID,
        questionnaires: list[OrganizationQuestionnaire],
        submissions_for: t.Callable[[OrganizationQuestionnaire], list[QuestionnaireSubmission] | None],
    ) -> None:
        """Initialize the check.

        Args:
            event_id: The event the resulting verdict is reported against.
            questionnaires: The applicable questionnaires (already filtered for exemptions).
            submissions_for: Returns the user's submissions for a questionnaire, newest first, or None.
        """
        self.event_id = event_id
        self.questionnaires = questionnaires
        self.submissions_for = submissions_for

    def check(self) -> EventUserEligibility | None:
        """Return the first failing verdict (missing, pending review, failed), or None if all is in order."""
        if missing := self._check_missing_questionnaires():
            return missing
        if pending_review := self._check_pending_review():
            return pending_review
        if failed := self._check_failed():
            return failed
        return None

    def _is_submission_expired(
        self,
        org_questionnaire: OrganizationQuestionnaire,
        submission: QuestionnaireSubmission,
    ) -> bool:
        """Check if an approved submission has expired based on max_submission_age.

        An expired submission means the user must retake the questionnaire.
        Only APPROVED submissions can expire - other states are handled by separate checks.

        Returns:
            True if submission has an APPROVED evaluation that has expired.
            False if no expiration configured, or submission is not approved yet.
        """
        evaluation: QuestionnaireEvaluation | None = getattr(submission, "evaluation", None)
        if evaluation is None:
            return False  # No evaluation yet - let _check_pending_review handle this

        if evaluation.status != QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED:
            return False  # Not approved - let _check_failed handle this

        return approval_is_stale(org_questionnaire.max_submission_age, evaluation.updated_at)

    def _check_retake_eligibility(
        self,
        questionnaire: Questionnaire,
        submissions: list[QuestionnaireSubmission],
        questionnaires_missing: list[uuid.UUID],
        failed_questionnaires: list[uuid.UUID],
    ) -> EventUserEligibility | None:
        """Apply the retake policy to a rejected questionnaire. Mutates both id lists."""
        submission = submissions[0]
        assert submission.submitted_at is not None  # Submissions with evaluations always have submitted_at
        decision = evaluate_retake_policy(
            questionnaire,
            last_submitted_at=submission.submitted_at,
            attempts=len(submissions),
        )
        if decision.verdict is RetakeVerdict.ATTEMPTS_EXHAUSTED:
            failed_questionnaires.append(questionnaire.id)
            return None
        if decision.verdict is RetakeVerdict.RETAKE_NOW:
            questionnaires_missing.append(questionnaire.id)
            return None
        return EventUserEligibility(
            allowed=False,
            reason=_(Reasons.QUESTIONNAIRE_FAILED),
            reason_code=Reasons.QUESTIONNAIRE_FAILED.code,
            event_id=self.event_id,
            next_step=NextStep.WAIT_TO_RETAKE_QUESTIONNAIRE,
            retry_on=decision.retry_on,
        )

    def _check_missing_questionnaires(self) -> EventUserEligibility | None:
        questionnaires_missing = []
        for org_questionnaire in self.questionnaires:
            # Look up the submission in our O(1) map. No database query.
            submissions = self.submissions_for(org_questionnaire)
            if (
                submissions is None
                or submissions[0].status != QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
            ):
                questionnaires_missing.append(org_questionnaire.questionnaire_id)
                continue

            # Check if the submission's approval has expired (only relevant when evaluation is required)
            if org_questionnaire.requires_evaluation and self._is_submission_expired(org_questionnaire, submissions[0]):
                questionnaires_missing.append(org_questionnaire.questionnaire_id)

        if questionnaires_missing:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event_id,
                reason=_(Reasons.QUESTIONNAIRE_MISSING),
                reason_code=Reasons.QUESTIONNAIRE_MISSING.code,
                next_step=NextStep.COMPLETE_QUESTIONNAIRE,
                questionnaires_missing=questionnaires_missing,
            )
        return None

    def _check_pending_review(self) -> EventUserEligibility | None:
        questionnaires_pending_review = []
        for org_questionnaire in self.questionnaires:
            if not org_questionnaire.requires_evaluation:
                continue
            # Look up the submission in our O(1) map. No database query.
            if submissions := self.submissions_for(org_questionnaire):
                evaluation = getattr(submissions[0], "evaluation", None)
                if (
                    evaluation is None
                    or evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW
                ):
                    questionnaires_pending_review.append(org_questionnaire.questionnaire_id)
        if questionnaires_pending_review:
            return EventUserEligibility(
                allowed=False,
                reason=_(Reasons.QUESTIONNAIRE_PENDING_REVIEW),
                reason_code=Reasons.QUESTIONNAIRE_PENDING_REVIEW.code,
                event_id=self.event_id,
                next_step=NextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION,
                questionnaires_pending_review=questionnaires_pending_review,
            )
        return None

    def _check_failed(self) -> EventUserEligibility | None:
        failed_questionnaires: list[uuid.UUID] = []
        questionnaires_missing: list[uuid.UUID] = []
        for org_questionnaire in self.questionnaires:
            if not org_questionnaire.requires_evaluation:
                continue
            # Look up the submission in our O(1) map. No database query.
            questionnaire = org_questionnaire.questionnaire
            submissions = self.submissions_for(org_questionnaire)
            # Skip if no submissions or no evaluation yet
            if not submissions or not (evaluation := getattr(submissions[0], "evaluation", None)):
                continue
            if evaluation.status != QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED:
                continue
            # At this point we have a rejected evaluation: the attempts cap and the
            # retake cooldown are both applied by the shared policy helper.
            if result := self._check_retake_eligibility(
                questionnaire, submissions, questionnaires_missing, failed_questionnaires
            ):
                return result
        if failed_questionnaires:
            return EventUserEligibility(
                allowed=False,
                reason=_(Reasons.QUESTIONNAIRE_FAILED),
                reason_code=Reasons.QUESTIONNAIRE_FAILED.code,
                event_id=self.event_id,
                questionnaires_failed=failed_questionnaires,
            )
        if questionnaires_missing:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event_id,
                reason=_(Reasons.QUESTIONNAIRE_MISSING),
                reason_code=Reasons.QUESTIONNAIRE_MISSING.code,
                next_step=NextStep.COMPLETE_QUESTIONNAIRE,
                questionnaires_missing=questionnaires_missing,
            )
        return None
