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
from PIL import Image, ImageOps

from .config import ModelThumbnailConfig, ThumbnailSpec


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
    The path is based on the target field name suffix.

    Args:
        original_path: Path to the original file in storage.
        field_name: Target field name (used to derive suffix).

    Returns:
        Path for the thumbnail file.

    Example:
        >>> get_thumbnail_path("logos/abc123.heic", "logo_thumbnail")
        'logos/abc123_thumbnail.jpg'
        >>> get_thumbnail_path("protected/profile-pictures/user/img.png", "profile_picture_preview")
        'protected/profile-pictures/user/img_preview.jpg'
    """
    path = Path(original_path)
    stem = path.stem
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


def delete_thumbnails_for_paths(paths: list[str]) -> None:
    """Delete thumbnail files from storage.

    Args:
        paths: List of thumbnail paths to delete.
    """
    for path in paths:
        if not path:
            continue
        try:
            if default_storage.exists(path):
                default_storage.delete(path)
                logger.info("thumbnail_deleted", path=path)
        except Exception as e:
            logger.error(
                "thumbnail_delete_failed",
                path=path,
                error=str(e),
            )


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
    """Delete an image file and all its derivative thumbnails/previews.

    This function should be used instead of direct `field.delete()` when removing
    images that have auto-generated derivatives. It:
    1. Looks up the thumbnail config for this model/field
    2. Deletes all derivative files from storage
    3. Clears all derivative field values on the model
    4. Deletes the source file from storage
    5. Saves the model with all cleared fields

    Args:
        instance: The Django model instance containing the image field.
        source_field: Name of the source image field (e.g., "logo", "profile_picture").

    Example:
        >>> delete_image_with_derivatives(user, "profile_picture")
        >>> delete_image_with_derivatives(organization, "logo")
    """
    from .config import get_thumbnail_config

    source_file = getattr(instance, source_field, None)
    if not source_file:
        return

    # Get model metadata for config lookup
    meta = instance._meta
    app_label = meta.app_label
    model_name = meta.model_name

    # Refresh the specific field from database to ensure we have the correct path.
    # Django's FileField can have stale in-memory values after partial saves.
    instance.refresh_from_db(fields=[source_field])
    source_file = getattr(instance, source_field, None)
    if not source_file:
        return

    # Look up thumbnail config (may be None for fields without configured derivatives,
    # in which case we just delete the source file without any derivative cleanup)
    config = get_thumbnail_config(app_label, model_name, source_field)

    # Store source path before clearing (needed for storage deletion)
    source_path = source_file.name if source_file.name else ""

    # Collect fields to clear and derivative paths
    fields_to_clear = [source_field]
    derivative_paths: list[str] = []

    if config:
        for spec in config.specs:
            field_name = spec.field_name
            derivative_file = getattr(instance, field_name, None)
            if derivative_file and derivative_file.name:
                derivative_paths.append(derivative_file.name)
            fields_to_clear.append(field_name)

    # Delete all files from storage (source + derivatives)
    delete_thumbnails_for_paths([source_path] + derivative_paths)

    # Clear all fields on the model
    for field_name in fields_to_clear:
        setattr(instance, field_name, None)

    # Save with all cleared fields
    instance.save(update_fields=fields_to_clear)

    logger.info(
        "image_with_derivatives_deleted",
        model=f"{app_label}.{model_name}",
        field=source_field,
        derivatives_deleted=len(derivative_paths),
    )
