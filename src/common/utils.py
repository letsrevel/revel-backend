import hashlib
import sys
import typing as t
from io import BytesIO

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import IntegrityError, models, transaction
from PIL import Image

from common import tasks

from .models import FileUploadAudit


def strip_exif(image_file: File) -> InMemoryUploadedFile:  # type: ignore[type-arg]
    """Strip EXIF data from a Django File or InMemoryUploadedFile."""
    image = Image.open(image_file)
    # Create a new image from raw pixel data to strip EXIF metadata
    image_no_exif = Image.frombytes(image.mode, image.size, image.tobytes())

    output = BytesIO()
    _format = image.format or "JPEG"
    image_no_exif.save(output, format=_format)
    output.seek(0)

    # Try to infer some optional fields
    field_name = getattr(image_file, "field_name", "image")
    name = getattr(image_file, "name", "image.jpg")
    content_type = getattr(image_file, "content_type", "image/jpeg")

    return InMemoryUploadedFile(
        output,
        field_name=field_name,
        name=name,
        content_type=content_type,
        size=sys.getsizeof(output),
        charset=None,
    )


def assert_image_equal(actual_bytes: bytes, expected_bytes: bytes) -> None:
    """Assert that two images are visually identical by comparing pixel data.

    Args:
        actual_bytes: The saved image bytes (e.g. from .read())
        expected_bytes: The original image bytes (e.g. uploaded or fixture)
    """
    img1 = Image.open(BytesIO(actual_bytes)).convert("RGB")
    img2 = Image.open(BytesIO(expected_bytes)).convert("RGB")

    assert img1.size == img2.size, f"Image size mismatch: {img1.size} vs {img2.size}"

    assert img1.tobytes() == img2.tobytes(), "Image pixel data mismatch"


T = t.TypeVar("T", bound=models.Model)


def get_or_create_with_race_protection(
    model: type[T],
    lookup_filter: models.Q,
    defaults: dict[str, t.Any],
) -> tuple[T, bool]:
    """Get or create a model instance with protection against race conditions.

    Attempts to retrieve an instance matching the lookup filter. If not found,
    creates one using the defaults. Handles IntegrityError from race conditions
    by retrying the lookup.

    Args:
        model: The Django model class
        lookup_filter: Q object for filtering the lookup
        defaults: Dictionary of field values for creating the instance

    Returns:
        Tuple of (instance, created) where created is True if the instance was created

    Example:
        food_item, created = get_or_create_with_race_protection(
            FoodItem,
            Q(name__iexact="peanuts"),
            {"name": "Peanuts"}
        )
    """
    manager: models.Manager[T] = getattr(model, "objects")
    instance = manager.filter(lookup_filter).first()
    if instance:
        return instance, False

    try:
        return manager.create(**defaults), True
    except IntegrityError:
        # Race condition: another request created it between our check and create
        instance = manager.filter(lookup_filter).first()
        if not instance:
            # Should never happen, but if it does, re-raise the original error
            raise
        return instance, False


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
    """
    FileUploadAudit.objects.create(
        app=app,
        model=model,
        instance_pk=instance_pk,
        field=field,
        file_hash=file_hash,
        uploader=uploader_email,
    )
    tasks.scan_for_malware.delay(app=app, model=model, pk=str(instance_pk), field=field)


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
    from common.thumbnails.config import THUMBNAIL_CONFIGS, get_thumbnail_field_names
    from common.thumbnails.tasks import (
        delete_orphaned_thumbnails_task,
        generate_thumbnails_task,
    )

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
        delete_orphaned_thumbnails_task.delay(thumbnail_paths=old_thumbnail_paths)

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
        generate_thumbnails_task.delay(
            app=app,
            model=model,
            pk=str(instance.pk),
            field=field,
        )

    return instance
