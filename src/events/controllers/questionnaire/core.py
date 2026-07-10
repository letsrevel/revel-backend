import typing as t
from uuid import UUID

from django.db.models import Prefetch, QuerySet
from ninja import Query
from ninja_extra import route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import searching

from common.authentication import I18nJWTAuth
from common.controllers import DistinctSearching
from common.schema import ValidationErrorResponse
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters
from events import models as event_models
from events import schema as event_schema
from events.service import event_questionnaire_service, update_organization_questionnaire
from events.service.event_questionnaire_service import duplicate_organization_questionnaire, get_questionnaire_summary
from questionnaires import models as questionnaires_models

from ..permissions import OrganizationPermission, QuestionnairePermission
from .base import QuestionnaireControllerBase


class QuestionnaireCoreMixin(QuestionnaireControllerBase):
    """Questionnaire-level CRUD: list, create, retrieve, update, status, delete, duplicate, summary."""

    @route.get(
        "/",
        url_name="list_org_questionnaires",
        response=PaginatedResponseSchema[event_schema.OrganizationQuestionnaireInListSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(DistinctSearching, search_fields=["questionnaire__name", "events__name", "event_series__name"])
    def list_org_questionnaires(
        self,
        params: t.Annotated[filters.QuestionnaireFilterSchema, Query(...)],
    ) -> QuerySet[event_models.OrganizationQuestionnaire]:
        """Browse questionnaires you have permission to view or manage.

        Returns questionnaires from organizations where you have staff/owner access. Use this to
        find questionnaires to attach to events or review submissions. Supports filtering by
        event_id or event_series_id to find questionnaires assigned to specific events or series.

        Each questionnaire includes a count of pending evaluations (submissions with no evaluation
        or evaluations with "pending review" status).
        """
        qs = event_questionnaire_service.annotate_pending_evaluations(self.get_admin_queryset())
        return params.filter(qs).distinct().order_by("-created_at")

    @route.post(
        "/{organization_id}/create-questionnaire",
        url_name="create_questionnaire",
        response={200: event_schema.OrganizationQuestionnaireSchema, 400: ValidationErrorResponse},
        auth=I18nJWTAuth(),
        permissions=[OrganizationPermission("create_questionnaire")],
    )
    def create_org_questionnaire(
        self, organization_id: UUID, payload: event_schema.OrganizationQuestionnaireCreateSchema
    ) -> event_models.OrganizationQuestionnaire:
        """Create a new questionnaire for an organization (admin only).

        Creates a questionnaire with specified type (admission, membership, feedback, or generic)
        and optional max_submission_age. After creation, add sections and questions via
        POST /questionnaires/{id}/sections and /multiple-choice-questions endpoints. Requires
        'create_questionnaire' permission (organization staff/owners).
        """
        organization = t.cast(
            event_models.Organization,
            self.get_object_or_exception(self.get_organization_queryset(), pk=organization_id),
        )
        return event_questionnaire_service.create_org_questionnaire(organization, payload)

    @route.get(
        "/{org_questionnaire_id}",
        url_name="get_org_questionnaire",
        response=event_schema.OrganizationQuestionnaireSchema,
        permissions=[QuestionnairePermission("evaluate_questionnaire")],
        throttle=UserDefaultThrottle(),
    )
    def get_org_questionnaire(self, org_questionnaire_id: UUID) -> event_models.OrganizationQuestionnaire:
        """Retrieve a questionnaire's details and structure (admin only).

        Returns the questionnaire with all sections, questions, and settings. Use this to view or
        edit an existing questionnaire. Requires permission to manage the organization's questionnaires.
        """
        # Prefetch with proper filtering to avoid duplicate questions in response
        qs = self.get_queryset().prefetch_related(
            Prefetch(
                "questionnaire__multiplechoicequestion_questions",
                queryset=questionnaires_models.MultipleChoiceQuestion.objects.filter(
                    section__isnull=True
                ).prefetch_related("options"),
            ),
            Prefetch(
                "questionnaire__freetextquestion_questions",
                queryset=questionnaires_models.FreeTextQuestion.objects.filter(section__isnull=True),
            ),
            Prefetch(
                "questionnaire__fileuploadquestion_questions",
                queryset=questionnaires_models.FileUploadQuestion.objects.filter(section__isnull=True),
            ),
            Prefetch(
                "questionnaire__sections",
                queryset=questionnaires_models.QuestionnaireSection.objects.prefetch_related(
                    Prefetch(
                        "multiplechoicequestion_questions",
                        queryset=questionnaires_models.MultipleChoiceQuestion.objects.prefetch_related("options"),
                    ),
                    "freetextquestion_questions",
                    "fileuploadquestion_questions",
                ).order_by("order"),
            ),
            "events",
            "event_series",
        )
        return t.cast(
            event_models.OrganizationQuestionnaire,
            self.get_object_or_exception(qs, pk=org_questionnaire_id),
        )

    @route.get(
        "/{org_questionnaire_id}/summary",
        url_name="questionnaire_summary",
        response=event_schema.QuestionnaireSummarySchema,
        permissions=[QuestionnairePermission("evaluate_questionnaire")],
        throttle=UserDefaultThrottle(),
    )
    def get_summary(
        self,
        org_questionnaire_id: UUID,
        event_id: UUID | None = None,
        event_series_id: UUID | None = None,
    ) -> event_schema.QuestionnaireSummarySchema:
        """Get aggregate statistics for a questionnaire's submissions.

        Returns status counts (per-submission and per-user), score stats, and
        per-MC-question answer distributions. Optionally filter by event or event series.
        Only one of event_id or event_series_id may be provided.
        Requires 'evaluate_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        return get_questionnaire_summary(
            questionnaire_id=org_questionnaire.questionnaire_id,
            event_id=event_id,
            event_series_id=event_series_id,
        )

    @route.put(
        "/{org_questionnaire_id}",
        url_name="update_org_questionnaire",
        response=event_schema.OrganizationQuestionnaireSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def update_org_questionnaire(
        self, org_questionnaire_id: UUID, payload: event_schema.OrganizationQuestionnaireUpdateSchema
    ) -> event_models.OrganizationQuestionnaire:
        """Update organization questionnaire and underlying questionnaire settings (admin only).

        Allows updating both OrganizationQuestionnaire wrapper fields (max_submission_age,
        questionnaire_type) and the underlying Questionnaire fields (name, min_score, llm_guidelines,
        shuffle_questions, shuffle_sections, evaluation_mode, can_retake_after, max_attempts).
        Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        return t.cast(
            event_models.OrganizationQuestionnaire, update_organization_questionnaire(org_questionnaire, payload)
        )

    @route.post(
        "/{org_questionnaire_id}/status/{status}",
        url_name="update_questionnaire_status",
        response=event_schema.OrganizationQuestionnaireSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def update_questionnaire_status(
        self, org_questionnaire_id: UUID, status: questionnaires_models.Questionnaire.QuestionnaireStatus
    ) -> event_models.OrganizationQuestionnaire:
        """Update the status of a questionnaire (admin only).

        Changes the questionnaire status between DRAFT, READY, and PUBLISHED.
        - DRAFT: Questionnaire is being created/edited
        - READY: Questionnaire is complete but not yet published
        - PUBLISHED: Questionnaire is live and can be taken by users

        Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = t.cast(
            event_models.OrganizationQuestionnaire,
            self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id),
        )
        return event_questionnaire_service.set_status(org_questionnaire, status)

    @route.delete(
        "/{org_questionnaire_id}",
        url_name="delete_org_questionnaire",
        response={204: None},
        permissions=[QuestionnairePermission("delete_questionnaire")],
    )
    def delete_org_questionnaire(self, org_questionnaire_id: UUID) -> tuple[int, None]:
        """Delete an organization questionnaire (admin only).

        Permanently removes the questionnaire. Requires 'delete_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        org_questionnaire.delete()
        return 204, None

    @route.post(
        "/{org_questionnaire_id}/duplicate",
        url_name="duplicate_org_questionnaire",
        response=event_schema.OrganizationQuestionnaireSchema,
        permissions=[QuestionnairePermission("create_questionnaire")],
        throttle=WriteThrottle(),
    )
    def duplicate_org_questionnaire(
        self, org_questionnaire_id: UUID, payload: event_schema.QuestionnaireDuplicateSchema
    ) -> event_models.OrganizationQuestionnaire:
        """Create a deep copy of a questionnaire within the same organization (admin only).

        Duplicates the questionnaire structure (sections, questions, options) into a new
        DRAFT questionnaire.  By default the copy is unattached to any events or event
        series; pass ``copy_associations: true`` to replicate the template's event/series
        links.

        Requires 'create_questionnaire' permission on the questionnaire's organization.
        """
        org_questionnaire = t.cast(
            event_models.OrganizationQuestionnaire,
            self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id),
        )
        return duplicate_organization_questionnaire(
            org_questionnaire,
            payload.name,
            copy_associations=payload.copy_associations,
        )
