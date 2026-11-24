"""Thumbnail generation service.

This module provides functions for generating and managing thumbnails for image fields.
Supports HEIC/HEIF formats via pillow-heif (registered in settings).
"""

import typing as t
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import structlog
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from PIL import Image, ImageOps

from .config import ModelThumbnailConfig, ThumbnailSpec
from .files import delete_unreferenced_files, lock_image_paths


@dataclass
class ThumbnailResult:
    """Result of thumbnail generation with partial failure tracking.

    Attributes:
        thumbnails: Dict mapping field_name -> saved thumbnail path for successful generations.
        failures: Dict mapping field_name -> error message for failed generations.
    """

    thumbnails: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def has_failures(self) -> bool:
        """Check if any thumbnail generations failed."""
        return len(self.failures) > 0

    @property
    def is_complete(self) -> bool:
        """Check if all thumbnails were generated successfully (no failures)."""
        return len(self.failures) == 0


logger = structlog.get_logger(__name__)


def get_thumbnail_path(original_path: str, field_name: str) -> str:
    """Generate thumbnail path from original path.

    Thumbnails are always stored as JPEG regardless of source format.
    The path is based on the source stem and extension and on the target field name suffix. The extension keeps
    the paths of two sources with the same stem apart (``avatar.png`` and a later ``avatar.jpg``).

    Args:
        original_path: Path to the original file in storage.
        field_name: Target field name (used to derive suffix).

    Returns:
        Path for the thumbnail file.

    Example:
        >>> get_thumbnail_path("logos/abc123.heic", "logo_thumbnail")
        'logos/abc123_heic_thumbnail.jpg'
        >>> get_thumbnail_path("protected/profile-pictures/user/img.png", "profile_picture_preview")
        'protected/profile-pictures/user/img_png_preview.jpg'
    """
    path = Path(original_path)
    extension = path.suffix.lstrip(".")  # not lowered: "a.PNG" and "a.png" are two files
    stem = f"{path.stem}_{extension}" if extension else path.stem
    parent = path.parent

    # Extract suffix from field name (e.g., "logo_thumbnail" -> "thumbnail")
    # For simple fields like "thumbnail" or "preview", use as-is
    if "_" in field_name:
        suffix = field_name.split("_")[-1]
    else:
        suffix = field_name

    return str(parent / f"{stem}_{suffix}.jpg")


def generate_thumbnail(image_source: bytes | t.IO[bytes], spec: ThumbnailSpec) -> bytes:
    """Generate a single thumbnail from image bytes or file handle.

    Args:
        image_source: Original image as bytes or a file-like object.
            Accepts IO[bytes] for memory-efficient streaming from storage.
        spec: Thumbnail specification with dimensions.

    Returns:
        JPEG bytes of the generated thumbnail.

    Raises:
        PIL.UnidentifiedImageError: If image cannot be read.
        OSError: If image processing fails.
    """
    # Normalize input: wrap bytes in BytesIO, use file handle directly
    source: t.IO[bytes]
    if isinstance(image_source, bytes):
        source = BytesIO(image_source)
    else:
        source = image_source

    with Image.open(source) as img:
        # Handle EXIF orientation before processing
        processed: Image.Image = _apply_exif_orientation(img)

        # Convert to RGB if necessary (HEIC, RGBA, P modes)
        if processed.mode in ("RGBA", "LA"):
            # Create white background for transparent images
            background = Image.new("RGB", processed.size, (255, 255, 255))
            background.paste(processed, mask=processed.split()[-1])
            processed = background
        elif processed.mode != "RGB":
            processed = processed.convert("RGB")

        # Use LANCZOS for high-quality downsampling
        processed.thumbnail((spec.max_width, spec.max_height), Image.Resampling.LANCZOS)

        output = BytesIO()
        processed.save(output, format="JPEG", quality=85, optimize=True)
        output.seek(0)
        return output.read()


def _apply_exif_orientation(img: Image.Image) -> Image.Image:
    """Apply EXIF orientation to image.

    Some images (especially from cameras/phones) store orientation in EXIF
    rather than in the actual pixel data. This ensures thumbnails are
    correctly oriented.

    Uses Pillow's built-in exif_transpose() which handles all orientation cases.

    Args:
        img: PIL Image object.

    Returns:
        Image with EXIF orientation applied.
    """
    return ImageOps.exif_transpose(img) or img


def generate_and_save_thumbnails(
    original_path: str,
    config: ModelThumbnailConfig,
) -> ThumbnailResult:
    """Generate and save all thumbnails for a file.

    Opens the source file separately for each thumbnail spec to minimize memory usage.
    This allows large images to be processed without loading the entire file into memory
    at once - PIL streams from the file handle as needed.

    Args:
        original_path: Path to original file in storage.
        config: Thumbnail configuration for this model/field.

    Returns:
        ThumbnailResult containing successful thumbnails and any failures.

    Raises:
        FileNotFoundError: If original file doesn't exist.
        PIL.UnidentifiedImageError: If file is not a valid image.
    """
    if not default_storage.exists(original_path):
        raise FileNotFoundError(f"Original file not found: {original_path}")

    result = ThumbnailResult()

    for spec in config.specs:
        try:
            # Open file for each spec to minimize memory usage.
            # PIL streams from the file handle, avoiding loading full image into memory.
            with default_storage.open(original_path, "rb") as f:
                thumb_data = generate_thumbnail(f, spec)

            thumb_path = get_thumbnail_path(original_path, spec.field_name)

            # Delete existing thumbnail if present (for regeneration)
            if default_storage.exists(thumb_path):
                default_storage.delete(thumb_path)

            saved_path = default_storage.save(
                thumb_path,
                ContentFile(thumb_data, name=Path(thumb_path).name),
            )
            result.thumbnails[spec.field_name] = saved_path

            logger.info(
                "thumbnail_generated",
                original=original_path,
                thumbnail=saved_path,
                field=spec.field_name,
                width=spec.max_width,
                height=spec.max_height,
            )
        except Exception as e:
            error_msg = str(e)
            result.failures[spec.field_name] = error_msg
            logger.error(
                "thumbnail_generation_failed",
                original=original_path,
                field=spec.field_name,
                error=error_msg,
                exc_info=True,
            )

    return result


def write_paths_if_source_unchanged(
    model_class: t.Any, pk: t.Any, field: str, original_path: str, thumbnails: dict[str, str]
) -> tuple[bool, list[str]]:
    """Under the row lock, write ``thumbnails`` if the row still holds ``original_path``.

    The row can change while the files are made: a new upload, a removed source, a deletion.

    Returns:
        Whether the files must be discarded, and the derived paths that the write replaced (an older path
        format, for example on a regeneration).
    """
    replaced: list[str] = []
    with transaction.atomic():
        locked = model_class.objects.select_for_update().filter(pk=pk).first()
        current = getattr(locked, field, None) if locked is not None else None
        if not current or current.name != original_path:
            return True, replaced
        update_fields = [name for name in thumbnails if hasattr(locked, name)]
        lock_image_paths([thumbnails[name] for name in update_fields])
        # A delete of unreferenced files (another row's discard) can have removed a file made here. The retry of
        # the task makes it again.
        if missing := [name for name in update_fields if not default_storage.exists(thumbnails[name])]:
            # The other files of this attempt go too (if no row names them); the retry makes them all again.
            delete_unreferenced_files([thumbnails[name] for name in update_fields])
            raise OSError(f"Derived files deleted meanwhile: {missing}")
        for field_name in update_fields:
            old = getattr(locked, field_name)
            if old and old.name != thumbnails[field_name]:
                replaced.append(old.name)
            setattr(locked, field_name, thumbnails[field_name])
        if update_fields:
            locked.save(update_fields=update_fields)
    return False, replaced


def is_image_mime_type(mime_type: str) -> bool:
    """Check if a MIME type is an image type that supports thumbnails.

    Args:
        mime_type: MIME type string.

    Returns:
        True if the MIME type is a supported image type.
    """
    supported_types = {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
        "image/heif",
    }
    return mime_type.lower() in supported_types


def delete_image_with_derivatives(
    instance: t.Any,
    source_field: str,
) -> None:
    """Remove an image and its derived images from the row, and delete the files after commit.

    Use it instead of ``field.delete()`` for an image with derived images. The paths are read under the row
    lock, so derived paths written meanwhile are included. A file that another row still names (a duplicated
    event shares its images) stays.

    Args:
        instance: The Django model instance containing the image field.
        source_field: Name of the source image field (e.g., "logo", "profile_picture").

    Example:
        >>> delete_image_with_derivatives(user, "profile_picture")
        >>> delete_image_with_derivatives(organization, "logo")
    """
    from .config import get_thumbnail_config
    from .files import delete_unreferenced_files_after_commit

    meta = instance._meta
    config = get_thumbnail_config(meta.app_label, meta.model_name, source_field)
    fields_to_clear = [source_field, *(spec.field_name for spec in config.specs)] if config else [source_field]

    with transaction.atomic():
        stored = type(instance).objects.select_for_update().filter(pk=instance.pk).values_list(*fields_to_clear).first()
        paths = [name for name in stored or () if name]
        if not stored or not stored[0]:
            return
        for field_name in fields_to_clear:
            setattr(instance, field_name, None)
        instance.save(update_fields=fields_to_clear)
        delete_unreferenced_files_after_commit(paths)

    logger.info(
        "image_with_derivatives_deleted",
        model=f"{meta.app_label}.{meta.model_name}",
        field=source_field,
        derivatives_deleted=len(paths) - 1,
    )


def replace_image_files_after_commit(instance: t.Any, source_field: str, old_paths: list[str]) -> None:
    """After commit, delete the old image files that no row names, and make the derived images of the new image.

    For a change that wrote ``source_field`` and cleared its derived fields (for example an admin form).

    Args:
        instance: The saved instance.
        source_field: The source image field that changed.
        old_paths: The stored paths of the source and its derived images before the change.
    """
    from .config import get_thumbnail_config
    from .tasks import delete_orphaned_thumbnails_task, generate_thumbnails_task

    meta = instance._meta
    app, model, pk = meta.app_label, meta.model_name, str(instance.pk)
    new_name = getattr(instance, source_field).name or ""
    old_paths = [name for name in old_paths if name and name != new_name]
    if old_paths:
        # robust: a broker failure must not fail the saved change; the old files then stay (harmless).
        transaction.on_commit(lambda: delete_orphaned_thumbnails_task.delay(thumbnail_paths=old_paths), robust=True)
    if new_name and get_thumbnail_config(app, model, source_field):
        transaction.on_commit(
            lambda: generate_thumbnails_task.delay(app=app, model=model, pk=pk, field=source_field), robust=True
        )
