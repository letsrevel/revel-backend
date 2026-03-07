"""Generic helpers for FileExport lifecycle transitions."""

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from django.utils.translation import gettext as _

from common.models import FileExport
from common.signing import generate_signed_url

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


def notify_export_ready(export: FileExport) -> None:
    """Send email notification that the export is ready for download."""
    from django.template.loader import render_to_string

    from common.tasks import send_email

    signed_path = generate_signed_url(export.file.name, expires_in=EXPORT_URL_EXPIRES_IN)
    download_url = settings.BASE_URL + signed_path
    user = export.requested_by

    subject = _("Your Revel Export is Ready")
    context = {
        "download_url": download_url,
        "display_name": user.get_display_name(),
        "export_type": export.get_export_type_display(),
    }
    body = render_to_string("events/emails/export_ready_body.txt", context)
    html_body = render_to_string("events/emails/export_ready_body.html", context)
    send_email.delay(to=user.email, subject=subject, body=body, html_body=html_body)
