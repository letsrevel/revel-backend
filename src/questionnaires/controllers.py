"""Controllers for questionnaire file management."""

import typing as t
from uuid import UUID

from django.db.models import QuerySet
from ninja.files import UploadedFile
from ninja.params import File
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle, WriteThrottle

from .models import QuestionnaireFile
from .schema import QuestionnaireFileSchema
from .service import file_service


@api_controller(
    "/questionnaire-files", auth=I18nJWTAuth(), tags=["Questionnaire Files"], throttle=UserDefaultThrottle()
)
class QuestionnaireFileController(UserAwareController):
    """Controller for managing user's questionnaire file uploads.

    Provides endpoints for uploading, listing, and deleting files that can be
    used as answers to file upload questions in questionnaires. Files are stored
    in a user-scoped library and can be reused across multiple questions.
    """

    def get_queryset(self) -> QuerySet[QuestionnaireFile]:
        """Get files belonging to the current user."""
        return QuestionnaireFile.objects.for_user(self.user())

    @route.post(
        "/",
        url_name="upload_questionnaire_file",
        response=QuestionnaireFileSchema,
        throttle=WriteThrottle(),
    )
    def upload_file(self, file: File[UploadedFile]) -> QuestionnaireFile:
        """Upload a file to your questionnaire file library.

        Files are deduplicated by content hash - uploading the same file twice returns
        the existing file. Uploaded files are scanned for malware asynchronously.

        The file is stored with a UUID filename to prevent enumeration attacks.
        Original filename is preserved in metadata for display purposes.
        """
        return file_service.upload_questionnaire_file(self.user(), file)

    @route.get(
        "/",
        url_name="list_questionnaire_files",
        response=PaginatedResponseSchema[QuestionnaireFileSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=50)
    def list_files(self) -> QuerySet[QuestionnaireFile]:
        """List all files in your questionnaire file library.

        Returns files you've uploaded, sorted by most recent first. Use these file IDs
        when submitting questionnaire answers for file upload questions.
        """
        return self.get_queryset().order_by("-created_at")

    @route.get(
        "/{file_id}",
        url_name="get_questionnaire_file",
        response=QuestionnaireFileSchema,
    )
    def get_file(self, file_id: UUID) -> QuestionnaireFile:
        """Get details of a specific file in your library."""
        return t.cast(
            QuestionnaireFile,
            self.get_object_or_exception(self.get_queryset(), pk=file_id),
        )

    @route.delete(
        "/{file_id}",
        url_name="delete_questionnaire_file",
        response={204: None},
        throttle=WriteThrottle(),
    )
    def delete_file(self, file_id: UUID) -> tuple[int, None]:
        """Delete a file from your questionnaire file library.

        Privacy Policy: Files are HARD DELETED immediately, including from storage.
        This applies even if the file was used in submitted questionnaires - user
        privacy takes precedence over data integrity. Submissions referencing deleted
        files will show the file as unavailable.

        This is intentional for GDPR/privacy compliance: users have the right to
        delete their uploaded content at any time.
        """
        file = self.get_object_or_exception(self.get_queryset(), pk=file_id)
        file.delete()  # Model's delete() method handles storage cleanup
        return 204, None
