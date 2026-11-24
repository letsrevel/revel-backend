"""File-upload service: validation, audit records, malware scan and thumbnail scheduling.

Lives in the service layer (rather than ``common.utils``) because it depends on
``common.models`` and ``common.tasks`` at module level, and ``common.models``
imports ``common.utils`` — keeping these helpers here breaks that cycle.
"""

import hashlib
import typing as t

import structlog
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction

from common import tasks
from common.models import FileUploadAudit
from common.thumbnails.config import THUMBNAIL_CONFIGS, get_thumbnail_field_names
from common.thumbnails.files import stored_image_paths
from common.thumbnails.tasks import (
    delete_orphaned_thumbnails_task,
    generate_thumbnails_task,
)

T = t.TypeVar("T", bound=models.Model)

logger = structlog.get_logger(__name__)


def _scan_dispatch(*, app: str, model: str, pk: str, field: str) -> t.Callable[[], None]:
    """A named on-commit callback that queues the malware scan of one upload.

    It runs after the upload is committed, so a broker failure must not fail the request. The
    failure is logged and the audit stays ``PENDING``: ``rescan_pending_uploads`` dispatches it
    again. It is a named function, not a ``partial``, so Django's robust handling can name it.
    """

    def dispatch_malware_scan() -> None:
        try:
            tasks.scan_for_malware.delay(app=app, model=model, pk=pk, field=field)
        except Exception:
            logger.warning("malware_scan_dispatch_failed", app=app, model=model, pk=pk, field=field, exc_info=True)

    return dispatch_malware_scan


def create_file_audit_and_scan(
    *,
    app: str,
    model: str,
    instance_pk: t.Any,
    field: str,
    file_hash: str,
    uploader_email: str,
    file_path: str = "",
) -> None:
    """Create audit record and schedule malware scan for an uploaded file.

    This helper consolidates the audit + scan logic used by both:
    - safe_save_uploaded_file (for replacing files on existing models)
    - questionnaire file uploads (for user file libraries)

    Args:
        app: Django app label (e.g., "events", "questionnaires")
        model: Model name (e.g., "organization", "questionnairefile")
        instance_pk: Primary key of the model instance
        field: Field name containing the file
        file_hash: SHA-256 hash of the file content
        uploader_email: Email of the user who uploaded the file
        file_path: Storage path of the uploaded file; the scan reads this file

    When ``FEATURE_MALWARE_SCAN`` is disabled the file is recorded as ``CLEAN``
    immediately and no ClamAV scan is dispatched (self-host without an AV daemon).

    The scan is queued after commit. If queuing fails, the upload is kept and the
    periodic ``rescan_pending_uploads`` task queues the scan again.
    """
    audit = FileUploadAudit.objects.create(
        app=app,
        model=model,
        instance_pk=instance_pk,
        field=field,
        file_hash=file_hash,
        uploader=uploader_email,
        file_path=file_path,
    )
    if not settings.FEATURE_MALWARE_SCAN:
        audit.status = FileUploadAudit.FileUploadAuditStatus.CLEAN
        audit.save(update_fields=["status", "updated_at"])
        return
    # The callback logs its own failure; robust is a backstop so a failed dispatch never skips other callbacks.
    transaction.on_commit(_scan_dispatch(app=app, model=model, pk=str(instance_pk), field=field), robust=True)


def _validate_file_field(instance: models.Model, field_name: str, file: File) -> None:  # type: ignore[type-arg]
    """Run validators for a file field before saving.

    Retrieves validators from the model field definition and runs them
    against the uploaded file. This ensures validation errors are raised
    BEFORE the model's save() method attempts EXIF stripping.

    Args:
        instance: The model instance being saved
        field_name: Name of the file field
        file: The uploaded file to validate

    Raises:
        ValidationError: If any validator fails, with errors keyed by field name.
            This is caught by the API exception handler and returned as 400.
    """
    model_field = instance._meta.get_field(field_name)
    errors: list[str] = []
    # File fields are always Field instances with validators (not ForeignObjectRel/GenericForeignKey)
    for validator in model_field.validators:  # type: ignore[union-attr]
        try:
            validator(file)
        except ValidationError as e:
            errors.extend(e.messages)
    if errors:
        raise ValidationError({field_name: errors})


@transaction.atomic
def safe_save_uploaded_file(
    *,
    instance: T,
    field: str,
    file: File,  # type: ignore[type-arg]
    uploader: AbstractUser,
) -> T:
    """Safely save an uploaded file passing it to malware scan.

    Validates the file against field validators before saving.
    After commit, deletes the old file and its derived images, if no other row names them.
    Schedules thumbnail generation for image files.

    Raises:
        ValidationError: If file validation fails (returned as 400 by API).
    """
    # Validate BEFORE setting the file or saving
    _validate_file_field(instance, field, file)

    app = instance._meta.app_label
    model = t.cast(str, instance._meta.model_name)
    config_key = (app, model, field)
    config = THUMBNAIL_CONFIGS.get(config_key)

    # The stored files of this image, read under the row lock; they are deleted after commit, and only when
    # no other row names them (a duplicated event shares its images). A rollback keeps them.
    thumbnail_field_names = get_thumbnail_field_names(config) if config else []
    if instance.pk is not None:
        old_paths = stored_image_paths(type(instance), instance.pk, field)
        if old_paths:
            # robust: a broker failure must not fail the stored upload (#1446); orphaned files are harmless.
            transaction.on_commit(lambda: delete_orphaned_thumbnails_task.delay(thumbnail_paths=old_paths), robust=True)
    for thumb_field in thumbnail_field_names:
        setattr(instance, thumb_field, None)

    setattr(instance, field, file)

    # Determine which fields to update
    update_fields = [field] + thumbnail_field_names

    instance.save(update_fields=update_fields)
    # Refresh from DB to get the actual storage path (not the original filename)
    # We refresh the entire instance to ensure all file field attributes are updated
    instance.refresh_from_db()
    file_field = getattr(instance, field)
    file_field.open()
    # Two uploads in quick succession: B reads A's stored paths under the row lock (they are deleted after B
    # commits), and A's thumbnail task finds that its source changed and discards its files.
    file_hash = hashlib.sha256(file_field.read()).hexdigest()
    file_field.seek(0)
    create_file_audit_and_scan(
        app=app,
        model=model,
        instance_pk=instance.pk,
        field=field,
        file_hash=file_hash,
        uploader_email=uploader.email,
        file_path=file_field.name,
    )

    # Schedule thumbnail generation if configured for this model/field
    if config:
        transaction.on_commit(
            lambda: generate_thumbnails_task.delay(
                app=app,
                model=model,
                pk=str(instance.pk),
                field=field,
            ),
            robust=True,  # a broker failure must not fail the stored upload (#1446)
        )

    return instance
