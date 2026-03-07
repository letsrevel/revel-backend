"""Schemas for file export endpoints."""

from ninja import ModelSchema

from common.models import FileExport
from common.service.export_service import EXPORT_URL_EXPIRES_IN
from common.signing import generate_signed_url


class FileExportSchema(ModelSchema):
    download_url: str | None = None

    class Meta:
        model = FileExport
        fields = ["id", "export_type", "status", "error_message", "completed_at", "created_at"]

    @staticmethod
    def resolve_download_url(obj: FileExport) -> str | None:
        """Return a signed download URL when the export is ready."""
        if obj.status == FileExport.ExportStatus.READY and obj.file:
            return generate_signed_url(obj.file.name, expires_in=EXPORT_URL_EXPIRES_IN)
        return None
