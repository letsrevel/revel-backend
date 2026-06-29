import typing as t
from uuid import UUID

from django.db.models import QuerySet
from ninja import Query
from ninja_extra import route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.schema import ValidationErrorResponse
from common.throttling import ExportThrottle, UserDefaultThrottle
from events import filters
from events import models as event_models
from events import schema as event_schema
from events.service import event_questionnaire_service, feedback_service
from questionnaires import models as questionnaires_models
from questionnaires import schema as questionnaire_schema
from questionnaires.service import SubmissionService

from ..permissions import QuestionnairePermission
from .base import QuestionnaireControllerBase

if t.TYPE_CHECKING:
    from common.models import FileExport


class QuestionnaireSubmissionsMixin(QuestionnaireControllerBase):
    """Submission review: export, list, detail and evaluation.

    Route order matters here: ``/submissions/export`` must be registered before
    ``/submissions/{submission_id}`` because path params compile to a plain
    ``str`` converter, so the literal ``export`` would otherwise be swallowed by
    the ``{submission_id}`` route (yielding a 405 on the export POST).
    """

    @route.post(
        "/{org_questionnaire_id}/submissions/export",
        url_name="export_submissions",
        response={202: event_schema.FileExportSchema},
        permissions=[QuestionnairePermission("evaluate_questionnaire")],
        throttle=ExportThrottle(),
    )
    def export_submissions(
        self,
        org_questionnaire_id: UUID,
        event_id: UUID | None = None,
        event_series_id: UUID | None = None,
    ) -> tuple[int, "FileExport"]:
        """Export questionnaire submissions as an Excel file (async).

        Triggers an async Celery task to generate the export. Returns a 202 with a FileExport
        resource that can be polled via GET /exports/{id} until the file is ready for download.

        Optionally filter by event_id or event_series_id (mutually exclusive).
        Requires 'evaluate_questionnaire' permission.
        """
        org_questionnaire = t.cast(
            event_models.OrganizationQuestionnaire,
            self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id),
        )
        export = event_questionnaire_service.start_submissions_export(
            org_questionnaire,
            requested_by=self.user(),
            event_id=event_id,
            event_series_id=event_series_id,
        )
        return 202, export

    @route.get(
        "/{org_questionnaire_id}/submissions",
        url_name="list_submissions",
        response=PaginatedResponseSchema[questionnaire_schema.SubmissionListItemSchema],
        permissions=[QuestionnairePermission("evaluate_questionnaire")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email", "user__first_name", "user__last_name"])
    def list_submissions(
        self,
        org_questionnaire_id: UUID,
        params: t.Annotated[filters.SubmissionFilterSchema, Query(...)],
        order_by: t.Literal["submitted_at", "-submitted_at"] = "-submitted_at",
    ) -> QuerySet[questionnaires_models.QuestionnaireSubmission]:
        """View user submissions for this questionnaire (admin only).

        Returns submitted questionnaires ready for review. Use this to see who has applied for
        event access and their responses. Requires 'evaluate_questionnaire' permission.

        Filtering:
        - evaluation_status: Filter by evaluation status (approved/rejected/pending review/no_evaluation)

        Ordering:
        - submitted_at: Oldest submissions first
        - -submitted_at: Newest submissions first (default)
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        return event_questionnaire_service.list_ready_submissions(org_questionnaire.questionnaire_id, params, order_by)

    @route.get(
        "/{org_questionnaire_id}/submissions/{submission_id}",
        url_name="get_submission_detail",
        response=questionnaire_schema.SubmissionDetailSchema,
        permissions=[QuestionnairePermission("evaluate_questionnaire")],
        throttle=UserDefaultThrottle(),
    )
    def get_submission_detail(
        self, org_questionnaire_id: UUID, submission_id: UUID
    ) -> questionnaires_models.QuestionnaireSubmission:
        """View detailed answers for a specific submission (admin only).

        Returns all questions and the user's answers, plus automatic evaluation results if available.
        Use this to review a submission before manual approval/rejection. Requires
        'evaluate_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        service = SubmissionService(org_questionnaire.questionnaire_id)
        return service.get_submission_detail(submission_id)

    @route.post(
        "/{org_questionnaire_id}/submissions/{submission_id}/evaluate",
        url_name="evaluate_submission",
        response={200: questionnaire_schema.EvaluationResponseSchema, 400: ValidationErrorResponse},
        permissions=[QuestionnairePermission("evaluate_questionnaire")],
    )
    def evaluate_submission(
        self,
        org_questionnaire_id: UUID,
        submission_id: UUID,
        payload: questionnaire_schema.EvaluationCreateSchema,
    ) -> questionnaires_models.QuestionnaireEvaluation:
        """Manually approve or reject a questionnaire submission (admin only).

        Overrides automatic evaluation or provides decision for manual-review questionnaires.
        Approved users can then RSVP or purchase tickets for the event. Requires
        'evaluate_questionnaire' permission.

        Note: Feedback questionnaires cannot be evaluated. If an admin changes a questionnaire
        type from FEEDBACK to ADMISSION, existing submissions would become evaluatable.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        feedback_service.validate_not_feedback_questionnaire_for_evaluation(org_questionnaire)
        service = SubmissionService(org_questionnaire.questionnaire_id)
        return service.evaluate_submission(submission_id, payload, self.user())
