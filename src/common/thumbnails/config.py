"""Thumbnail configuration for image fields.

This module defines thumbnail specifications for all image fields that support
thumbnail generation. Configuration is centralized here for easy maintenance.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ThumbnailSpec:
    """Specification for a single thumbnail size.

    Attributes:
        field_name: Name of the model field to store the thumbnail path.
        max_width: Maximum width in pixels.
        max_height: Maximum height in pixels.
    """

    field_name: str
    max_width: int
    max_height: int


@dataclass(frozen=True)
class ModelThumbnailConfig:
    """Configuration for thumbnail generation for a model field.

    Attributes:
        app_label: Django app label (e.g., "events", "accounts").
        model_name: Model name in lowercase (e.g., "organization", "reveluser").
        source_field: Name of the source image field (e.g., "logo", "profile_picture").
        specs: Tuple of ThumbnailSpec defining sizes to generate.
    """

    app_label: str
    model_name: str
    source_field: str
    specs: tuple[ThumbnailSpec, ...]


# Centralized configuration for all thumbnail-enabled fields
# Key: (app_label, model_name, source_field)
THUMBNAIL_CONFIGS: dict[tuple[str, str, str], ModelThumbnailConfig] = {
    # QuestionnaireFile.file
    ("questionnaires", "questionnairefile", "file"): ModelThumbnailConfig(
        app_label="questionnaires",
        model_name="questionnairefile",
        source_field="file",
        specs=(
            ThumbnailSpec("thumbnail", 150, 150),
            ThumbnailSpec("preview", 800, 800),
        ),
    ),
    # RevelUser.profile_picture
    ("accounts", "reveluser", "profile_picture"): ModelThumbnailConfig(
        app_label="accounts",
        model_name="reveluser",
        source_field="profile_picture",
        specs=(
            ThumbnailSpec("profile_picture_thumbnail", 150, 150),
            ThumbnailSpec("profile_picture_preview", 400, 400),
        ),
    ),
    # Organization.logo
    ("events", "organization", "logo"): ModelThumbnailConfig(
        app_label="events",
        model_name="organization",
        source_field="logo",
        specs=(ThumbnailSpec("logo_thumbnail", 150, 150),),
    ),
    # Organization.cover_art
    ("events", "organization", "cover_art"): ModelThumbnailConfig(
        app_label="events",
        model_name="organization",
        source_field="cover_art",
        specs=(
            ThumbnailSpec("cover_art_thumbnail", 150, 150),
            ThumbnailSpec("cover_art_social", 1200, 630),
        ),
    ),
    # Event.logo
    ("events", "event", "logo"): ModelThumbnailConfig(
        app_label="events",
        model_name="event",
        source_field="logo",
        specs=(ThumbnailSpec("logo_thumbnail", 150, 150),),
    ),
    # Event.cover_art
    ("events", "event", "cover_art"): ModelThumbnailConfig(
        app_label="events",
        model_name="event",
        source_field="cover_art",
        specs=(
            ThumbnailSpec("cover_art_thumbnail", 150, 150),
            ThumbnailSpec("cover_art_social", 1200, 630),
        ),
    ),
    # EventSeries.logo
    ("events", "eventseries", "logo"): ModelThumbnailConfig(
        app_label="events",
        model_name="eventseries",
        source_field="logo",
        specs=(ThumbnailSpec("logo_thumbnail", 150, 150),),
    ),
    # EventSeries.cover_art
    ("events", "eventseries", "cover_art"): ModelThumbnailConfig(
        app_label="events",
        model_name="eventseries",
        source_field="cover_art",
        specs=(
            ThumbnailSpec("cover_art_thumbnail", 150, 150),
            ThumbnailSpec("cover_art_social", 1200, 630),
        ),
    ),
}


def get_thumbnail_config(app_label: str, model_name: str, source_field: str) -> ModelThumbnailConfig | None:
    """Get thumbnail configuration for a model field.

    Args:
        app_label: Django app label.
        model_name: Model name (lowercase).
        source_field: Source image field name.

    Returns:
        ModelThumbnailConfig if configured, None otherwise.
    """
    return THUMBNAIL_CONFIGS.get((app_label, model_name, source_field))


def get_thumbnail_field_names(config: ModelThumbnailConfig) -> list[str]:
    """Get list of thumbnail field names for a config.

    Args:
        config: The thumbnail configuration.

    Returns:
        List of field names that store thumbnail paths.
    """
    return [spec.field_name for spec in config.specs]
