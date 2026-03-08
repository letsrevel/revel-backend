"""Generic helpers for FileExport lifecycle transitions."""

from django.core.files.base import ContentFile
from django.utils import timezone

from common.models import FileExport

EXPORT_URL_EXPIRES_IN = 7 * 24 * 3600  # 7 days


def start_export(export: FileExport) -> None:
    """Transition export to PROCESSING."""
    export.status = FileExport.ExportStatus.PROCESSING
    export.save(update_fields=["status", "updated_at"])


def complete_export(export: FileExport, file_bytes: bytes, filename: str) -> None:
    """Save the generated file and mark the export as READY."""
    export.file.save(filename, ContentFile(file_bytes), save=False)
    export.status = FileExport.ExportStatus.READY
    export.completed_at = timezone.now()
    export.save(update_fields=["file", "status", "completed_at", "updated_at"])


def fail_export(export: FileExport, error: str) -> None:
    """Mark the export as FAILED with an error message."""
    export.status = FileExport.ExportStatus.FAILED
    export.error_message = error
    export.save(update_fields=["status", "error_message", "updated_at"])
