"""Celery tasks for async thumbnail generation.

These tasks handle thumbnail generation in the background to avoid
blocking upload requests.
"""

import typing as t

import structlog
from celery import shared_task
from django.apps import apps

from .config import THUMBNAIL_CONFIGS
from .service import delete_thumbnails_for_paths, generate_and_save_thumbnails

logger = structlog.get_logger(__name__)


class ThumbnailConfigError(Exception):
    """Raised when thumbnail configuration is missing or invalid."""


class ThumbnailTargetNotFoundError(Exception):
    """Raised when the target instance or file is not found."""


class ThumbnailGenerationError(Exception):
    """Raised when one or more thumbnails fail to generate."""


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(OSError,),
)
def generate_thumbnails_task(
    self: t.Any,
    *,
    app: str,
    model: str,
    pk: str,
    field: str,
) -> dict[str, str]:
    """Generate thumbnails for an image field asynchronously.

    This task is idempotent - running it multiple times will regenerate
    thumbnails, which is safe and useful for recovery from failures.

    Args:
        self: Celery task instance (for retries).
        app: Django app label.
        model: Model name (lowercase).
        pk: Primary key of the instance.
        field: Name of the source image field.

    Returns:
        Dict of field_name -> thumbnail path.

    Raises:
        ThumbnailConfigError: If no thumbnail config exists for the model/field.
        ThumbnailTargetNotFoundError: If instance or file doesn't exist.
        FileNotFoundError: If the source file doesn't exist in storage.
        OSError: For transient storage errors (will retry).
    """
    config_key = (app, model, field)
    config = THUMBNAIL_CONFIGS.get(config_key)

    if not config:
        raise ThumbnailConfigError(f"No thumbnail configuration found for {app}.{model}.{field}")

    # Let LookupError propagate - it's a bug if model doesn't exist
    model_class = apps.get_model(app, model)

    try:
        instance = model_class.objects.get(pk=pk)
    except model_class.DoesNotExist as e:
        raise ThumbnailTargetNotFoundError(f"Instance {app}.{model} pk={pk} not found") from e

    file_field = getattr(instance, field, None)

    if not file_field:
        raise ThumbnailTargetNotFoundError(f"Field '{field}' is empty on {app}.{model} pk={pk}")

    original_path = file_field.name

    # Generate thumbnails - raises FileNotFoundError if file missing
    # OSError for storage issues will trigger automatic retry
    result = generate_and_save_thumbnails(original_path, config)

    # Update model with thumbnail paths on separate fields
    update_fields = []
    for field_name, path in result.thumbnails.items():
        if hasattr(instance, field_name):
            setattr(instance, field_name, path)
            update_fields.append(field_name)

    if update_fields:
        instance.save(update_fields=update_fields)

    logger.info(
        "thumbnails_generated_and_saved",
        app=app,
        model=model,
        pk=pk,
        field=field,
        thumbnails=result.thumbnails,
    )

    if result.has_failures:
        raise ThumbnailGenerationError(f"Failed to generate thumbnails for {app}.{model} pk={pk}: {result.failures}")

    return result.thumbnails


@shared_task
def delete_orphaned_thumbnails_task(
    *,
    thumbnail_paths: list[str],
) -> None:
    """Delete thumbnails when original is deleted.

    This task is fire-and-forget - we don't retry if deletion fails,
    as the files will be orphaned but not cause any functional issues.

    Args:
        thumbnail_paths: List of thumbnail paths to delete.
    """
    if thumbnail_paths:
        delete_thumbnails_for_paths(thumbnail_paths)
        logger.info(
            "orphaned_thumbnails_deleted",
            count=len(thumbnail_paths),
            paths=thumbnail_paths,
        )
