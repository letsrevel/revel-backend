"""File-related models: async exports, upload audits, and quarantined files."""

from django.conf import settings
from django.db import models

from common.fields import ProtectedFileField
from common.models.base import TimeStampedModel


class FileExport(TimeStampedModel):
    """Tracks async file export jobs (e.g. Excel exports)."""

    class ExportStatus(models.TextChoices):
        PENDING = "PENDING"
        PROCESSING = "PROCESSING"
        READY = "READY"
        FAILED = "FAILED"

    class ExportType(models.TextChoices):
        QUESTIONNAIRE_SUBMISSIONS = "questionnaire_submissions"
        ATTENDEE_LIST = "attendee_list"
        REVENUE_VAT_REPORT = "revenue_vat_report"

    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="file_exports")
    export_type = models.CharField(max_length=40, choices=ExportType.choices, db_index=True)
    status = models.CharField(max_length=20, choices=ExportStatus.choices, default=ExportStatus.PENDING, db_index=True)
    file = ProtectedFileField(upload_to="exports/", null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    parameters = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"FileExport({self.export_type}, {self.status})"


class FileUploadAudit(TimeStampedModel):
    class FileUploadAuditStatus(models.TextChoices):
        PENDING = "PENDING"
        CLEAN = "CLEAN"
        MALICIOUS = "MALICIOUS"

    app = models.CharField(max_length=64, db_index=True)
    model = models.CharField(max_length=64, db_index=True)
    instance_pk = models.UUIDField(db_index=True)
    field = models.CharField(max_length=64, db_index=True)
    file_hash = models.CharField(max_length=64, db_index=True)
    uploader = models.EmailField(db_index=True)
    status = models.CharField(
        choices=FileUploadAuditStatus.choices, max_length=20, db_index=True, default=FileUploadAuditStatus.PENDING
    )
    notified = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"{self.app}.{self.model}.{self.field} ({self.status})"


class QuarantinedFile(TimeStampedModel):
    audit = models.OneToOneField(FileUploadAudit, on_delete=models.CASCADE)
    # ProtectedFileField stores under protected/ so the file is only reachable via a
    # signed, authorized URL (Caddy forward_auth) — never from the public /media/* path.
    # Quarantined files hold the original (malicious / possibly PII-bearing) bytes and must
    # not be world-readable like the previous plain FileField allowed.
    file = ProtectedFileField(upload_to="quarantined_files")
    findings = models.JSONField()

    def __str__(self) -> str:
        return f"Quarantined: {self.audit_id}"
