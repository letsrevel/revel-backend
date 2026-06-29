from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import route

from events import models as event_models
from questionnaires import models as questionnaires_models
from questionnaires import schema as questionnaire_schema
from questionnaires.service import QuestionnaireService

from ..permissions import QuestionnairePermission
from .base import QuestionnaireControllerBase


class QuestionnaireSectionsMixin(QuestionnaireControllerBase):
    """Section CRUD for a questionnaire."""

    @route.post(
        "/{org_questionnaire_id}/sections",
        url_name="create_section",
        response=questionnaire_schema.SectionResponseSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def create_section(
        self, org_questionnaire_id: UUID, payload: questionnaire_schema.SectionCreateSchema
    ) -> questionnaires_models.QuestionnaireSection:
        """Add a section to organize questions in the questionnaire (admin only).

        Sections group related questions. Specify section name and display order. Requires
        'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        return service.create_section(payload)

    @route.put(
        "/{org_questionnaire_id}/sections/{section_id}",
        url_name="update_section",
        response=questionnaire_schema.SectionResponseSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def update_section(
        self, org_questionnaire_id: UUID, section_id: UUID, payload: questionnaire_schema.SectionUpdateSchema
    ) -> questionnaires_models.QuestionnaireSection:
        """Update a questionnaire section's details (admin only).

        Modify section name or display order. Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        section = get_object_or_404(
            questionnaires_models.QuestionnaireSection,
            pk=section_id,
            questionnaire_id=org_questionnaire.questionnaire_id,
        )
        return service.update_section(section, payload)

    @route.delete(
        "/{org_questionnaire_id}/sections/{section_id}",
        url_name="delete_section",
        response={204: None},
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def delete_section(self, org_questionnaire_id: UUID, section_id: UUID) -> tuple[int, None]:
        """Delete a questionnaire section (admin only).

        Removes the section and all questions within it. Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        section = get_object_or_404(
            questionnaires_models.QuestionnaireSection,
            pk=section_id,
            questionnaire_id=org_questionnaire.questionnaire_id,
        )
        section.delete()
        return 204, None
