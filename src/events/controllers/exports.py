"""Export status polling endpoint."""

from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.models import FileExport
from common.throttling import UserDefaultThrottle
from events import schema


@api_controller("/exports", auth=I18nJWTAuth(), tags=["Exports"], throttle=UserDefaultThrottle())
class ExportController(UserAwareController):
    @route.get(
        "/{export_id}",
        url_name="get_export_status",
        response=schema.FileExportSchema,
    )
    def get_export_status(self, export_id: UUID) -> FileExport:
        """Poll export status and get download URL when ready.

        Users can only access their own exports.
        """
        return get_object_or_404(FileExport, pk=export_id, requested_by=self.user())
