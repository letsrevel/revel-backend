"""Celery tasks for async thumbnail generation.

These tasks handle thumbnail generation in the background to avoid
blocking upload requests.
"""

import typing as t

import structlog
from celery import shared_task
from django.apps import apps

from .config import THUMBNAIL_CONFIGS
from .files import delete_unreferenced_files
from .service import generate_and_save_thumbnails, write_paths_if_source_unchanged

logger = structlog.get_logger(__name__)


class ThumbnailConfigError(Exception):
    """Raised when thumbnail configuration is missing or invalid."""


class ThumbnailTargetNotFoundError(Exception):
    """Raised when the target instance or file is not found."""


class ThumbnailGenerationError(Exception):
    """Raised when one or more thumbnails fail to generate."""


@shared_task(
    name="common.thumbnails.tasks.generate_thumbnails_task",
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

    # Write the paths only while the row still holds this source. The row can change while the files are made:
    # a new upload, a removed picture, a deletion. Then the files made here belong to nothing.
    # ponytail: the check compares file names, so a new upload with the same stored name is not detected; its own
    # task then writes the paths again. A source token (for example the file hash) would close that gap.
    discard, replaced = write_paths_if_source_unchanged(model_class, pk, field, original_path, result.thumbnails)
    if discard:
        delete_unreferenced_files(list(result.thumbnails.values()))
        logger.info("thumbnails_discarded_source_changed", app=app, model=model, pk=pk, field=field)
        return {}
    if replaced:  # after the commit above: the row no longer names these files
        delete_unreferenced_files(replaced)

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


@shared_task(name="common.thumbnails.tasks.delete_orphaned_thumbnails_task")
def delete_orphaned_thumbnails_task(
    *,
    thumbnail_paths: list[str],
) -> None:
    """Delete image files that a change removed from their row; files that another row still names stay.

    This task is fire-and-forget - we don't retry if deletion fails,
    as the files will be orphaned but not cause any functional issues.

    Args:
        thumbnail_paths: List of thumbnail paths to delete.
    """
    if thumbnail_paths:
        delete_unreferenced_files(thumbnail_paths)
        logger.info(
            "orphaned_thumbnails_deleted",
            count=len(thumbnail_paths),
            paths=thumbnail_paths,
        )
