"""Member-facing endpoints for the questionnaire that gates joining an organization."""

from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.schema import ErrorDetail
from common.throttling import QuestionnaireSubmissionThrottle, UserDefaultThrottle
from events.models import Organization, OrganizationQuestionnaire
from events.service import membership_questionnaire_service
from questionnaires.schema import (
    QuestionnaireSchema,
    QuestionnaireSubmissionOrEvaluationSchema,
    QuestionnaireSubmissionResponseSchema,
    QuestionnaireSubmissionSchema,
)
from questionnaires.service import SubmissionService


@api_controller("/me", auth=I18nJWTAuth(), tags=["Me - Applications"], throttle=UserDefaultThrottle())
class MeMembershipQuestionnaireController(UserAwareController):
    """Fetch and submit the membership questionnaire surfaced by join eligibility."""

    def _resolve(self, slug: str, questionnaire_id: UUID) -> OrganizationQuestionnaire:
        """Resolve the membership questionnaire wrapper, 404-ing on anything unreachable.

        Org visibility mirrors ``/join-eligibility``: an invisible org is a 404
        rather than a 200 confirming its existence to slug-guessing callers.
        """
        organization = get_object_or_404(Organization.objects.for_user(self.user()), slug=slug)
        return membership_questionnaire_service.get_membership_org_questionnaire(organization, questionnaire_id)

    @route.get(
        "/organizations/{slug}/membership-questionnaire/{questionnaire_id}",
        url_name="get_membership_questionnaire",
        response=QuestionnaireSchema,
    )
    def get_membership_questionnaire(self, slug: str, questionnaire_id: UUID) -> QuestionnaireSchema:
        """Retrieve the membership questionnaire that gates joining this organization.

        The `questionnaire_id` is the one returned by `GET /me/organizations/{slug}/join-eligibility`
        (or `POST /apply`) when `next_step=submit_questionnaire`. Returns 404 unless the
        questionnaire is the org's default membership questionnaire or one of its tiers'
        overrides — unrelated org questionnaires are not reachable here.
        """
        self._resolve(slug, questionnaire_id)
        # Seed the shuffle per viewer so the order is stable across page loads (#509).
        return SubmissionService(questionnaire_id).build(shuffle_seed=f"{questionnaire_id}:{self.user().pk}")

    @route.post(
        "/organizations/{slug}/membership-questionnaire/{questionnaire_id}/submit",
        url_name="submit_membership_questionnaire",
        response={200: QuestionnaireSubmissionOrEvaluationSchema, 400: ErrorDetail},
        throttle=QuestionnaireSubmissionThrottle(),
    )
    def submit_membership_questionnaire(
        self, slug: str, questionnaire_id: UUID, submission: QuestionnaireSubmissionSchema
    ) -> QuestionnaireSubmissionOrEvaluationSchema:
        """Submit answers to the organization's membership questionnaire.

        Validates that all mandatory questions are answered. A `ready` submission triggers
        automatic evaluation (LLM for free-text answers) when the questionnaire requires it;
        otherwise it awaits staff review. Returns 400 when the retake policy forbids another
        attempt (pending evaluation, already approved, cooldown not elapsed, attempts exhausted).

        Poll `GET /me/organizations/{slug}/join-eligibility` (or `GET /me/applications/{id}`)
        afterwards to advance the application; the returned submission id may also be passed to
        `POST /me/organizations/{slug}/apply` as `questionnaire_submission_id`.
        """
        org_questionnaire = self._resolve(slug, questionnaire_id)
        db_submission = membership_questionnaire_service.submit_membership_questionnaire(
            user=self.user(),
            org_questionnaire=org_questionnaire,
            questionnaire_service=SubmissionService(questionnaire_id),
            submission_schema=submission,
        )
        return QuestionnaireSubmissionResponseSchema.from_orm(db_submission)
