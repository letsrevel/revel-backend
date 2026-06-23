"""Poll-scoped question/section/option CRUD endpoints.

Mirrors :mod:`events.controllers.questionnaire` but writes to the bare
:class:`questionnaires.models.Questionnaire` that a :class:`polls.models.Poll`
wraps (polls don't go through :class:`events.models.OrganizationQuestionnaire`,
so the existing ``/questionnaires/{org_questionnaire_id}/...`` endpoints don't
apply).

Lockdown is enforced by signals in :mod:`polls.signals`: any write to a
question/section/option on a non-DRAFT poll raises
:class:`polls.exceptions.PollQuestionLockedError`, dispatched to HTTP
**423 Locked** by :mod:`polls.exception_handlers`. The controller therefore
needs no explicit status check.

.. note::

   **Canonical reference**: :class:`events.controllers.questionnaire.QuestionnaireController`.
   Both controllers expose the same 15 question/section/option CRUD endpoints,
   differing only in URL prefix (``/polls/{poll_id}/...`` vs
   ``/questionnaires/{org_questionnaire_id}/...``), permission class
   (``PollPermission`` vs ``QuestionnairePermission``), and parent-lookup
   queryset. Request/response schemas and underlying service calls are
   identical — both delegate to :class:`questionnaires.service.QuestionnaireService`.

   When editing routes here, mirror the change in ``QuestionnaireController``
   (and vice versa). The drift-detection test
   :func:`polls.tests.test_controller_drift.test_polls_and_org_questionnaire_question_crud_in_sync`
   fails CI if the two sets diverge in URL shape or HTTP method.
"""

import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import WriteThrottle
from polls.models import Poll
from polls.permissions import PollPermission
from questionnaires import models as questionnaires_models
from questionnaires import schema as questionnaire_schema
from questionnaires.service import QuestionnaireService


@api_controller(
    "/polls",
    tags=["Polls"],
    auth=I18nJWTAuth(),
    throttle=WriteThrottle(),
    permissions=[PollPermission("manage_polls")],
)
class PollQuestionController(UserAwareController):
    """Poll-scoped question/option/section CRUD.

    All endpoints fetch the poll via ``self._poll_queryset()``; the
    :class:`PollPermission` permission class enforces ``manage_polls`` against
    the resolved instance. Writes flow through
    :class:`questionnaires.service.QuestionnaireService` on the wrapped
    ``Questionnaire``; DELETE calls ``.delete()`` on the fetched model (no
    service method exists for deletion).
    """

    def _poll_queryset(self) -> QuerySet[Poll]:
        """Visibility-filtered queryset used by every endpoint."""
        return Poll.objects.for_user(self.user()).select_related("questionnaire")

    # ------------------------------------------------------------------ sections

    @route.post(
        "/{poll_id}/sections",
        url_name="poll_create_section",
        response=questionnaire_schema.SectionResponseSchema,
    )
    def create_section(
        self, poll_id: UUID, payload: questionnaire_schema.SectionCreateSchema
    ) -> questionnaires_models.QuestionnaireSection:
        """Add a section to a DRAFT poll's questionnaire."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        return service.create_section(payload)

    @route.put(
        "/{poll_id}/sections/{section_id}",
        url_name="poll_update_section",
        response=questionnaire_schema.SectionResponseSchema,
    )
    def update_section(
        self, poll_id: UUID, section_id: UUID, payload: questionnaire_schema.SectionUpdateSchema
    ) -> questionnaires_models.QuestionnaireSection:
        """Update a section on a DRAFT poll's questionnaire."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        section = get_object_or_404(
            questionnaires_models.QuestionnaireSection,
            pk=section_id,
            questionnaire_id=poll.questionnaire_id,
        )
        return service.update_section(section, payload)

    @route.delete(
        "/{poll_id}/sections/{section_id}",
        url_name="poll_delete_section",
        response={204: None},
    )
    def delete_section(self, poll_id: UUID, section_id: UUID) -> tuple[int, None]:
        """Delete a section (and its nested questions) from a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        section = get_object_or_404(
            questionnaires_models.QuestionnaireSection,
            pk=section_id,
            questionnaire_id=poll.questionnaire_id,
        )
        section.delete()
        return 204, None

    # ----------------------------------------------------- multiple-choice questions

    @route.post(
        "/{poll_id}/multiple-choice-questions",
        url_name="poll_create_mc_question",
        response=questionnaire_schema.MultipleChoiceQuestionResponseSchema,
    )
    def create_mc_question(
        self, poll_id: UUID, payload: questionnaire_schema.MultipleChoiceQuestionCreateSchema
    ) -> questionnaires_models.MultipleChoiceQuestion:
        """Add a multiple-choice question to a DRAFT poll's questionnaire."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        return service.create_mc_question(payload)

    @route.put(
        "/{poll_id}/multiple-choice-questions/{question_id}",
        url_name="poll_update_mc_question",
        response=questionnaire_schema.MultipleChoiceQuestionResponseSchema,
    )
    def update_mc_question(
        self,
        poll_id: UUID,
        question_id: UUID,
        payload: questionnaire_schema.MultipleChoiceQuestionUpdateSchema,
    ) -> questionnaires_models.MultipleChoiceQuestion:
        """Update a multiple-choice question on a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        mc_question = get_object_or_404(
            questionnaires_models.MultipleChoiceQuestion,
            pk=question_id,
            questionnaire_id=poll.questionnaire_id,
        )
        return service.update_mc_question(mc_question, payload)

    @route.delete(
        "/{poll_id}/multiple-choice-questions/{question_id}",
        url_name="poll_delete_mc_question",
        response={204: None},
    )
    def delete_mc_question(self, poll_id: UUID, question_id: UUID) -> tuple[int, None]:
        """Delete a multiple-choice question (and its options) from a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        question = get_object_or_404(
            questionnaires_models.MultipleChoiceQuestion,
            pk=question_id,
            questionnaire_id=poll.questionnaire_id,
        )
        question.delete()
        return 204, None

    # ----------------------------------------------------- multiple-choice options

    @route.post(
        "/{poll_id}/multiple-choice-questions/{question_id}/options",
        url_name="poll_create_mc_option",
        response=questionnaire_schema.MultipleChoiceOptionUpdateSchema,
    )
    def create_mc_option(
        self,
        poll_id: UUID,
        question_id: UUID,
        payload: questionnaire_schema.MultipleChoiceOptionCreateSchema,
    ) -> questionnaires_models.MultipleChoiceOption:
        """Add an option to an MC question on a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        question = get_object_or_404(
            questionnaires_models.MultipleChoiceQuestion,
            id=question_id,
            questionnaire_id=poll.questionnaire_id,
        )
        return service.create_mc_option(question, payload)

    @route.put(
        "/{poll_id}/multiple-choice-options/{option_id}",
        url_name="poll_update_mc_option",
        response=questionnaire_schema.MultipleChoiceOptionUpdateSchema,
    )
    def update_mc_option(
        self,
        poll_id: UUID,
        option_id: UUID,
        payload: questionnaire_schema.MultipleChoiceOptionUpdateSchema,
    ) -> questionnaires_models.MultipleChoiceOption:
        """Update an MC option on a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        option = get_object_or_404(
            questionnaires_models.MultipleChoiceOption,
            id=option_id,
            question__questionnaire_id=poll.questionnaire_id,
        )
        return service.update_mc_option(option, payload)

    @route.delete(
        "/{poll_id}/multiple-choice-options/{option_id}",
        url_name="poll_delete_mc_option",
        response={204: None},
    )
    def delete_mc_option(self, poll_id: UUID, option_id: UUID) -> tuple[int, None]:
        """Delete an MC option from a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        option = get_object_or_404(
            questionnaires_models.MultipleChoiceOption,
            pk=option_id,
            question__questionnaire_id=poll.questionnaire_id,
        )
        option.delete()
        return 204, None

    # ----------------------------------------------------- free-text questions

    @route.post(
        "/{poll_id}/free-text-questions",
        url_name="poll_create_ft_question",
        response=questionnaire_schema.FreeTextQuestionResponseSchema,
    )
    def create_ft_question(
        self, poll_id: UUID, payload: questionnaire_schema.FreeTextQuestionCreateSchema
    ) -> questionnaires_models.FreeTextQuestion:
        """Add a free-text question to a DRAFT poll's questionnaire."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        return service.create_ft_question(payload)

    @route.put(
        "/{poll_id}/free-text-questions/{question_id}",
        url_name="poll_update_ft_question",
        response=questionnaire_schema.FreeTextQuestionResponseSchema,
    )
    def update_ft_question(
        self,
        poll_id: UUID,
        question_id: UUID,
        payload: questionnaire_schema.FreeTextQuestionUpdateSchema,
    ) -> questionnaires_models.FreeTextQuestion:
        """Update a free-text question on a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        ft_question = get_object_or_404(
            questionnaires_models.FreeTextQuestion,
            id=question_id,
            questionnaire_id=poll.questionnaire_id,
        )
        return service.update_ft_question(ft_question, payload)

    @route.delete(
        "/{poll_id}/free-text-questions/{question_id}",
        url_name="poll_delete_ft_question",
        response={204: None},
    )
    def delete_ft_question(self, poll_id: UUID, question_id: UUID) -> tuple[int, None]:
        """Delete a free-text question from a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        question = get_object_or_404(
            questionnaires_models.FreeTextQuestion,
            pk=question_id,
            questionnaire_id=poll.questionnaire_id,
        )
        question.delete()
        return 204, None

    # ----------------------------------------------------- file-upload questions

    @route.post(
        "/{poll_id}/file-upload-questions",
        url_name="poll_create_fu_question",
        response=questionnaire_schema.FileUploadQuestionResponseSchema,
    )
    def create_fu_question(
        self, poll_id: UUID, payload: questionnaire_schema.FileUploadQuestionCreateSchema
    ) -> questionnaires_models.FileUploadQuestion:
        """Add a file-upload question to a DRAFT poll's questionnaire."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        return service.create_fu_question(payload)

    @route.put(
        "/{poll_id}/file-upload-questions/{question_id}",
        url_name="poll_update_fu_question",
        response=questionnaire_schema.FileUploadQuestionResponseSchema,
    )
    def update_fu_question(
        self,
        poll_id: UUID,
        question_id: UUID,
        payload: questionnaire_schema.FileUploadQuestionUpdateSchema,
    ) -> questionnaires_models.FileUploadQuestion:
        """Update a file-upload question on a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        service = QuestionnaireService(poll.questionnaire_id)
        fu_question = get_object_or_404(
            questionnaires_models.FileUploadQuestion,
            id=question_id,
            questionnaire_id=poll.questionnaire_id,
        )
        return service.update_fu_question(fu_question, payload)

    @route.delete(
        "/{poll_id}/file-upload-questions/{question_id}",
        url_name="poll_delete_fu_question",
        response={204: None},
    )
    def delete_fu_question(self, poll_id: UUID, question_id: UUID) -> tuple[int, None]:
        """Delete a file-upload question from a DRAFT poll."""
        poll = t.cast(Poll, self.get_object_or_exception(self._poll_queryset(), pk=poll_id))
        question = get_object_or_404(
            questionnaires_models.FileUploadQuestion,
            pk=question_id,
            questionnaire_id=poll.questionnaire_id,
        )
        question.delete()
        return 204, None


__all__ = ["PollQuestionController"]
