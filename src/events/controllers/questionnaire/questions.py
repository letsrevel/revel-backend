from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import route

from events import models as event_models
from questionnaires import models as questionnaires_models
from questionnaires import schema as questionnaire_schema
from questionnaires.service import QuestionnaireService

from ..permissions import QuestionnairePermission
from .base import QuestionnaireControllerBase


class QuestionnaireQuestionsMixin(QuestionnaireControllerBase):
    """Multiple-choice, free-text and file-upload question (and option) CRUD."""

    @route.post(
        "/{org_questionnaire_id}/multiple-choice-questions",
        url_name="create_mc_question",
        response=questionnaire_schema.MultipleChoiceQuestionResponseSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def create_mc_question(
        self, org_questionnaire_id: UUID, payload: questionnaire_schema.MultipleChoiceQuestionCreateSchema
    ) -> questionnaires_models.MultipleChoiceQuestion:
        """Add a multiple-choice question to the questionnaire (admin only).

        Create a question with predefined answer options. After creation, add options via
        POST /questionnaires/{id}/multiple-choice-questions/{question_id}/options. Requires
        'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        return service.create_mc_question(payload)

    @route.put(
        "/{org_questionnaire_id}/multiple-choice-questions/{question_id}",
        url_name="update_mc_question",
        response=questionnaire_schema.MultipleChoiceQuestionResponseSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def update_mc_question(
        self,
        org_questionnaire_id: UUID,
        question_id: UUID,
        payload: questionnaire_schema.MultipleChoiceQuestionUpdateSchema,
    ) -> questionnaires_models.MultipleChoiceQuestion:
        """Create a multiple choice question."""
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        mc_question = get_object_or_404(
            questionnaires_models.MultipleChoiceQuestion,
            pk=question_id,
            questionnaire_id=org_questionnaire.questionnaire_id,
        )
        return service.update_mc_question(mc_question, payload)

    @route.delete(
        "/{org_questionnaire_id}/multiple-choice-questions/{question_id}",
        url_name="delete_mc_question",
        response={204: None},
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def delete_mc_question(self, org_questionnaire_id: UUID, question_id: UUID) -> tuple[int, None]:
        """Delete a multiple choice question (admin only).

        Removes the question and all its options. Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        question = get_object_or_404(
            questionnaires_models.MultipleChoiceQuestion,
            pk=question_id,
            questionnaire_id=org_questionnaire.questionnaire_id,
        )
        question.delete()
        return 204, None

    @route.post(
        "/{org_questionnaire_id}/multiple-choice-questions/{question_id}/options",
        url_name="create_mc_option",
        response=questionnaire_schema.MultipleChoiceOptionUpdateSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def create_mc_option(
        self,
        org_questionnaire_id: UUID,
        question_id: UUID,
        payload: questionnaire_schema.MultipleChoiceOptionCreateSchema,
    ) -> questionnaires_models.MultipleChoiceOption:
        """Create a multiple choice question."""
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        question = get_object_or_404(
            questionnaires_models.MultipleChoiceQuestion,
            id=question_id,
            questionnaire_id=org_questionnaire.questionnaire_id,
        )
        return service.create_mc_option(question, payload)

    @route.put(
        "/{org_questionnaire_id}/multiple-choice-options/{option_id}",
        url_name="update_mc_option",
        response=questionnaire_schema.MultipleChoiceOptionUpdateSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def update_mc_option(
        self,
        org_questionnaire_id: UUID,
        option_id: UUID,
        payload: questionnaire_schema.MultipleChoiceOptionUpdateSchema,
    ) -> questionnaires_models.MultipleChoiceOption:
        """Create a multiple choice question."""
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        option = get_object_or_404(
            questionnaires_models.MultipleChoiceOption,
            id=option_id,
            question__questionnaire_id=org_questionnaire.questionnaire_id,
        )
        return service.update_mc_option(option, payload)

    @route.delete(
        "/{org_questionnaire_id}/multiple-choice-options/{option_id}",
        url_name="delete_mc_option",
        response={204: None},
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def delete_mc_option(self, org_questionnaire_id: UUID, option_id: UUID) -> tuple[int, None]:
        """Delete a multiple choice option (admin only).

        Removes the option from a question. Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        option = get_object_or_404(
            questionnaires_models.MultipleChoiceOption,
            pk=option_id,
            question__questionnaire_id=org_questionnaire.questionnaire_id,
        )
        option.delete()
        return 204, None

    @route.post(
        "/{org_questionnaire_id}/free-text-questions",
        url_name="create_ft_question",
        response=questionnaire_schema.FreeTextQuestionResponseSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def create_ft_question(
        self, org_questionnaire_id: UUID, payload: questionnaire_schema.FreeTextQuestionCreateSchema
    ) -> questionnaires_models.FreeTextQuestion:
        """Add a free-text question to the questionnaire (admin only).

        Create an open-ended question for text responses. Can be auto-evaluated by LLM based on
        scoring criteria. Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        return service.create_ft_question(payload)

    @route.put(
        "/{org_questionnaire_id}/free-text-questions/{question_id}",
        url_name="update_ft_question",
        response=questionnaire_schema.FreeTextQuestionResponseSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def update_ft_question(
        self, org_questionnaire_id: UUID, question_id: UUID, payload: questionnaire_schema.FreeTextQuestionUpdateSchema
    ) -> questionnaires_models.FreeTextQuestion:
        """Create a multiple choice question."""
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        ft_question = get_object_or_404(
            questionnaires_models.FreeTextQuestion, id=question_id, questionnaire_id=org_questionnaire.questionnaire_id
        )
        return service.update_ft_question(ft_question, payload)

    @route.delete(
        "/{org_questionnaire_id}/free-text-questions/{question_id}",
        url_name="delete_ft_question",
        response={204: None},
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def delete_ft_question(self, org_questionnaire_id: UUID, question_id: UUID) -> tuple[int, None]:
        """Delete a free text question (admin only).

        Removes the question. Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        question = get_object_or_404(
            questionnaires_models.FreeTextQuestion,
            pk=question_id,
            questionnaire_id=org_questionnaire.questionnaire_id,
        )
        question.delete()
        return 204, None

    @route.post(
        "/{org_questionnaire_id}/file-upload-questions",
        url_name="create_fu_question",
        response=questionnaire_schema.FileUploadQuestionResponseSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def create_fu_question(
        self, org_questionnaire_id: UUID, payload: questionnaire_schema.FileUploadQuestionCreateSchema
    ) -> questionnaires_models.FileUploadQuestion:
        """Add a file upload question to the questionnaire (admin only).

        Create a question that accepts file/image uploads. Configure allowed MIME types,
        max file size, and max number of files. File uploads are treated as informational
        by default (no automatic scoring). Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        return service.create_fu_question(payload)

    @route.put(
        "/{org_questionnaire_id}/file-upload-questions/{question_id}",
        url_name="update_fu_question",
        response=questionnaire_schema.FileUploadQuestionResponseSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def update_fu_question(
        self,
        org_questionnaire_id: UUID,
        question_id: UUID,
        payload: questionnaire_schema.FileUploadQuestionUpdateSchema,
    ) -> questionnaires_models.FileUploadQuestion:
        """Update a file upload question (admin only).

        Modify question text, allowed MIME types, max file size, or max files. Requires
        'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(
            event_models.OrganizationQuestionnaire, pk=org_questionnaire_id
        )
        service = QuestionnaireService(org_questionnaire.questionnaire_id)
        fu_question = get_object_or_404(
            questionnaires_models.FileUploadQuestion,
            id=question_id,
            questionnaire_id=org_questionnaire.questionnaire_id,
        )
        return service.update_fu_question(fu_question, payload)

    @route.delete(
        "/{org_questionnaire_id}/file-upload-questions/{question_id}",
        url_name="delete_fu_question",
        response={204: None},
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def delete_fu_question(self, org_questionnaire_id: UUID, question_id: UUID) -> tuple[int, None]:
        """Delete a file upload question (admin only).

        Removes the question. Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        question = get_object_or_404(
            questionnaires_models.FileUploadQuestion,
            pk=question_id,
            questionnaire_id=org_questionnaire.questionnaire_id,
        )
        question.delete()
        return 204, None
