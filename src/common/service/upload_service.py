"""File-upload service: validation, audit records, malware scan and thumbnail scheduling.

Lives in the service layer (rather than ``common.utils``) because it depends on
``common.models`` and ``common.tasks`` at module level, and ``common.models``
imports ``common.utils`` — keeping these helpers here breaks that cycle.
"""

import hashlib
import typing as t

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction

from common import tasks
from common.models import FileUploadAudit
from common.thumbnails.config import THUMBNAIL_CONFIGS, get_thumbnail_field_names
from common.thumbnails.tasks import (
    delete_orphaned_thumbnails_task,
    generate_thumbnails_task,
)

T = t.TypeVar("T", bound=models.Model)


def create_file_audit_and_scan(
    *,
    app: str,
    model: str,
    instance_pk: t.Any,
    field: str,
    file_hash: str,
    uploader_email: str,
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

    When ``FEATURE_MALWARE_SCAN`` is disabled the file is recorded as ``CLEAN``
    immediately and no ClamAV scan is dispatched (self-host without an AV daemon).
    """
    audit = FileUploadAudit.objects.create(
        app=app,
        model=model,
        instance_pk=instance_pk,
        field=field,
        file_hash=file_hash,
        uploader=uploader_email,
    )
    if not settings.FEATURE_MALWARE_SCAN:
        audit.status = FileUploadAudit.FileUploadAuditStatus.CLEAN
        audit.save(update_fields=["status", "updated_at"])
        return
    transaction.on_commit(lambda: tasks.scan_for_malware.delay(app=app, model=model, pk=str(instance_pk), field=field))


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
    Deletes the old file if one exists before saving the new file.
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

    # Collect old thumbnail paths for deletion
    old_thumbnail_paths: list[str] = []
    thumbnail_field_names: list[str] = []
    if config:
        thumbnail_field_names = get_thumbnail_field_names(config)
        for thumb_field in thumbnail_field_names:
            if hasattr(instance, thumb_field):
                thumb_file = getattr(instance, thumb_field, None)
                # ImageField returns an ImageFieldFile - get path via .name
                if thumb_file and hasattr(thumb_file, "name") and thumb_file.name:
                    old_thumbnail_paths.append(thumb_file.name)
                # Clear the field (None for ImageField)
                setattr(instance, thumb_field, None)

    # Delete old file if it exists
    old_file = getattr(instance, field)
    if old_file:
        old_file.delete(save=False)

    # Schedule deletion of old thumbnails
    if old_thumbnail_paths:
        transaction.on_commit(lambda: delete_orphaned_thumbnails_task.delay(thumbnail_paths=old_thumbnail_paths))

    setattr(instance, field, file)

    # Determine which fields to update
    update_fields = [field] + thumbnail_field_names

    instance.save(update_fields=update_fields)
    # Refresh from DB to get the actual storage path (not the original filename)
    # We refresh the entire instance to ensure all file field attributes are updated
    instance.refresh_from_db()
    file_field = getattr(instance, field)
    file_field.open()
    # NOTE: Race condition possible if user uploads twice in rapid succession.
    # File A upload: deletes old thumbnails async, schedules thumbnail generation
    # File B upload (before A's thumbnails exist): deletes nothing, schedules generation
    # Result: A's thumbnails may be orphaned. Accepted risk - orphaned files are harmless
    # and a cleanup task can handle them periodically if needed.
    file_hash = hashlib.sha256(file_field.read()).hexdigest()
    file_field.seek(0)
    create_file_audit_and_scan(
        app=app,
        model=model,
        instance_pk=instance.pk,
        field=field,
        file_hash=file_hash,
        uploader_email=uploader.email,
    )

    # Schedule thumbnail generation if configured for this model/field
    if config:
        transaction.on_commit(
            lambda: generate_thumbnails_task.delay(
                app=app,
                model=model,
                pk=str(instance.pk),
                field=field,
            )
        )

    return instance
