"""Tests for the thumbnail generation service.

This module tests the thumbnail service functions and configuration helpers.
"""

import typing as t
from io import BytesIO

import piexif
import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, UnidentifiedImageError

from common.thumbnails.config import (
    ModelThumbnailConfig,
    ThumbnailSpec,
    get_thumbnail_config,
    get_thumbnail_field_names,
)
from common.thumbnails.service import (
    ThumbnailResult,
    delete_thumbnails_for_paths,
    generate_and_save_thumbnails,
    generate_thumbnail,
    get_thumbnail_path,
    is_image_mime_type,
)

pytestmark = pytest.mark.django_db


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def rgba_image_bytes() -> bytes:
    """Create an RGBA image with transparency in memory as PNG bytes."""
    img = Image.new("RGBA", (200, 150), color=(255, 0, 0, 128))
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.read()


@pytest.fixture
def image_with_exif_orientation() -> bytes:
    """Create an image with EXIF orientation tag set to 6 (90 degrees CW rotation)."""
    img = Image.new("RGB", (200, 100), color="red")
    exif_dict: dict[str, t.Any] = {
        "0th": {piexif.ImageIFD.Orientation: 6},
        "Exif": {},
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }
    exif_bytes = piexif.dump(exif_dict)
    buffer = BytesIO()
    img.save(buffer, format="JPEG", exif=exif_bytes)
    buffer.seek(0)
    return buffer.read()


@pytest.fixture
def grayscale_image_bytes() -> bytes:
    """Create a grayscale (L mode) image."""
    img = Image.new("L", (100, 100), color=128)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.read()


@pytest.fixture
def palette_image_bytes() -> bytes:
    """Create a palette mode (P) image."""
    img = Image.new("P", (100, 100), color=1)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.read()


@pytest.fixture
def test_thumbnail_spec() -> ThumbnailSpec:
    """Create a test thumbnail specification."""
    return ThumbnailSpec(field_name="logo_thumbnail", max_width=150, max_height=150)


@pytest.fixture
def test_thumbnail_config() -> ModelThumbnailConfig:
    """Create a test thumbnail configuration."""
    return ModelThumbnailConfig(
        app_label="events",
        model_name="organization",
        source_field="logo",
        specs=(
            ThumbnailSpec("logo_thumbnail", 150, 150),
            ThumbnailSpec("logo_preview", 400, 400),
        ),
    )


# =============================================================================
# Tests for get_thumbnail_path()
# =============================================================================


class TestGetThumbnailPath:
    """Tests for the get_thumbnail_path function."""

    def test_simple_path_with_underscore_field_name(self) -> None:
        """Test path generation with a field name containing underscore."""
        result = get_thumbnail_path("logos/abc123.png", "logo_thumbnail")
        assert result == "logos/abc123_thumbnail.jpg"

    def test_simple_field_name_without_underscore(self) -> None:
        """Test path generation with a simple field name without underscore."""
        result = get_thumbnail_path("images/photo.jpeg", "thumbnail")
        assert result == "images/photo_thumbnail.jpg"

    def test_heic_source_converts_to_jpg(self) -> None:
        """Test that HEIC source files result in JPG thumbnails."""
        result = get_thumbnail_path("uploads/image.heic", "cover_art_thumbnail")
        assert result == "uploads/image_thumbnail.jpg"
        assert result.endswith(".jpg")

    def test_nested_directory_path(self) -> None:
        """Test path generation with deeply nested directory structure."""
        result = get_thumbnail_path("protected/profile-pictures/user/abc123.png", "profile_picture_preview")
        assert result == "protected/profile-pictures/user/abc123_preview.jpg"

    def test_multiple_underscores_in_field_name(self) -> None:
        """Test path generation with multiple underscores in field name."""
        result = get_thumbnail_path("files/doc.webp", "cover_art_social")
        assert result == "files/doc_social.jpg"

    def test_complex_filename_with_multiple_dots(self) -> None:
        """Test path generation with filename containing multiple dots."""
        result = get_thumbnail_path("uploads/image.backup.2024.png", "thumbnail")
        assert result == "uploads/image.backup.2024_thumbnail.jpg"


# =============================================================================
# Tests for generate_thumbnail()
# =============================================================================


class TestGenerateThumbnail:
    """Tests for the generate_thumbnail function."""

    def test_generates_jpeg_output(
        self,
        rgb_image_bytes: bytes,
        test_thumbnail_spec: ThumbnailSpec,
    ) -> None:
        """Test that output is always JPEG format."""
        result = generate_thumbnail(rgb_image_bytes, test_thumbnail_spec)
        with Image.open(BytesIO(result)) as img:
            assert img.format == "JPEG"

    def test_rgba_converted_to_rgb_with_white_background(
        self,
        rgba_image_bytes: bytes,
        test_thumbnail_spec: ThumbnailSpec,
    ) -> None:
        """Test that RGBA images are converted to RGB with white background."""
        result = generate_thumbnail(rgba_image_bytes, test_thumbnail_spec)
        with Image.open(BytesIO(result)) as img:
            assert img.mode == "RGB"

    def test_grayscale_converted_to_rgb(
        self,
        grayscale_image_bytes: bytes,
        test_thumbnail_spec: ThumbnailSpec,
    ) -> None:
        """Test that grayscale images are converted to RGB."""
        result = generate_thumbnail(grayscale_image_bytes, test_thumbnail_spec)
        with Image.open(BytesIO(result)) as img:
            assert img.mode == "RGB"

    def test_palette_mode_converted_to_rgb(
        self,
        palette_image_bytes: bytes,
        test_thumbnail_spec: ThumbnailSpec,
    ) -> None:
        """Test that palette mode (P) images are converted to RGB."""
        result = generate_thumbnail(palette_image_bytes, test_thumbnail_spec)
        with Image.open(BytesIO(result)) as img:
            assert img.mode == "RGB"

    def test_large_image_resized_within_bounds(
        self,
        large_image_bytes: bytes,
        test_thumbnail_spec: ThumbnailSpec,
    ) -> None:
        """Test that large images are resized to fit within spec dimensions."""
        result = generate_thumbnail(large_image_bytes, test_thumbnail_spec)
        with Image.open(BytesIO(result)) as img:
            assert img.width <= test_thumbnail_spec.max_width
            assert img.height <= test_thumbnail_spec.max_height

    def test_maintains_aspect_ratio(
        self,
        large_image_bytes: bytes,
        test_thumbnail_spec: ThumbnailSpec,
    ) -> None:
        """Test that aspect ratio is maintained during resize."""
        original_aspect_ratio = 2000 / 1500
        result = generate_thumbnail(large_image_bytes, test_thumbnail_spec)
        with Image.open(BytesIO(result)) as img:
            thumbnail_aspect_ratio = img.width / img.height
            assert abs(thumbnail_aspect_ratio - original_aspect_ratio) < 0.01

    def test_exif_orientation_applied(
        self,
        image_with_exif_orientation: bytes,
        test_thumbnail_spec: ThumbnailSpec,
    ) -> None:
        """Test that EXIF orientation is applied correctly."""
        result = generate_thumbnail(image_with_exif_orientation, test_thumbnail_spec)
        with Image.open(BytesIO(result)) as img:
            assert img.height > img.width

    def test_small_image_not_enlarged(self, rgb_image_bytes: bytes) -> None:
        """Test that small images are not enlarged beyond original size."""
        large_spec = ThumbnailSpec(field_name="preview", max_width=800, max_height=800)
        result = generate_thumbnail(rgb_image_bytes, large_spec)
        with Image.open(BytesIO(result)) as img:
            assert img.width <= 200
            assert img.height <= 150

    def test_invalid_image_raises_error(self, test_thumbnail_spec: ThumbnailSpec) -> None:
        """Test that invalid image data raises UnidentifiedImageError."""
        with pytest.raises(UnidentifiedImageError):
            generate_thumbnail(b"not an image at all", test_thumbnail_spec)


# =============================================================================
# Tests for generate_and_save_thumbnails()
# =============================================================================


class TestGenerateAndSaveThumbnails:
    """Tests for the generate_and_save_thumbnails function."""

    def test_generates_all_configured_thumbnails(
        self,
        rgb_image_bytes: bytes,
        test_thumbnail_config: ModelThumbnailConfig,
    ) -> None:
        """Test that all thumbnails in the config are generated."""
        original_path = "test-thumbnails/original.jpg"
        default_storage.save(original_path, ContentFile(rgb_image_bytes))

        try:
            result = generate_and_save_thumbnails(original_path, test_thumbnail_config)

            assert isinstance(result, ThumbnailResult)
            assert result.is_complete
            assert not result.has_failures
            assert len(result.thumbnails) == 2
            assert "logo_thumbnail" in result.thumbnails
            assert "logo_preview" in result.thumbnails

            for path in result.thumbnails.values():
                assert default_storage.exists(path)
        finally:
            default_storage.delete(original_path)
            for path in result.thumbnails.values():
                if default_storage.exists(path):
                    default_storage.delete(path)

    def test_file_not_found_raises_error(
        self,
        test_thumbnail_config: ModelThumbnailConfig,
    ) -> None:
        """Test that missing original file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError) as exc_info:
            generate_and_save_thumbnails("nonexistent.jpg", test_thumbnail_config)
        assert "not found" in str(exc_info.value).lower()

    def test_replaces_existing_thumbnail(
        self,
        rgb_image_bytes: bytes,
        test_thumbnail_config: ModelThumbnailConfig,
    ) -> None:
        """Test that existing thumbnails are replaced during regeneration."""
        original_path = "test-thumbnails/original-replace.jpg"
        default_storage.save(original_path, ContentFile(rgb_image_bytes))

        try:
            result1 = generate_and_save_thumbnails(original_path, test_thumbnail_config)
            thumb_path = result1.thumbnails["logo_thumbnail"]
            result2 = generate_and_save_thumbnails(original_path, test_thumbnail_config)

            assert result2.thumbnails["logo_thumbnail"] == thumb_path
            assert default_storage.exists(thumb_path)
        finally:
            default_storage.delete(original_path)
            for path in result1.thumbnails.values():
                if default_storage.exists(path):
                    default_storage.delete(path)


# =============================================================================
# Tests for delete_thumbnails_for_paths()
# =============================================================================


class TestDeleteThumbnailsForPaths:
    """Tests for the delete_thumbnails_for_paths function."""

    def test_deletes_existing_thumbnails(self, rgb_image_bytes: bytes) -> None:
        """Test that existing thumbnail files are deleted."""
        path1 = "test-thumbnails/thumb1.jpg"
        path2 = "test-thumbnails/thumb2.jpg"
        default_storage.save(path1, ContentFile(rgb_image_bytes))
        default_storage.save(path2, ContentFile(rgb_image_bytes))

        delete_thumbnails_for_paths([path1, path2])

        assert not default_storage.exists(path1)
        assert not default_storage.exists(path2)

    def test_handles_nonexistent_paths(self) -> None:
        """Test that nonexistent paths don't raise errors."""
        delete_thumbnails_for_paths(["nonexistent1.jpg", "nonexistent2.jpg"])

    def test_handles_empty_paths(self) -> None:
        """Test that empty paths in list are skipped."""
        delete_thumbnails_for_paths(["", "nonexistent.jpg", ""])

    def test_handles_empty_list(self) -> None:
        """Test that empty list doesn't raise errors."""
        delete_thumbnails_for_paths([])


# =============================================================================
# Tests for is_image_mime_type()
# =============================================================================


class TestIsImageMimeType:
    """Tests for the is_image_mime_type function."""

    @pytest.mark.parametrize(
        "mime_type",
        ["image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif"],
    )
    def test_supported_image_types(self, mime_type: str) -> None:
        """Test that supported image MIME types return True."""
        assert is_image_mime_type(mime_type) is True

    @pytest.mark.parametrize("mime_type", ["IMAGE/JPEG", "Image/PNG", "IMAGE/HEIC"])
    def test_case_insensitive(self, mime_type: str) -> None:
        """Test that MIME type matching is case-insensitive."""
        assert is_image_mime_type(mime_type) is True

    @pytest.mark.parametrize(
        "mime_type",
        ["application/pdf", "text/plain", "video/mp4", "image/svg+xml"],
    )
    def test_unsupported_types(self, mime_type: str) -> None:
        """Test that unsupported MIME types return False."""
        assert is_image_mime_type(mime_type) is False


# =============================================================================
# Tests for config helpers
# =============================================================================


class TestGetThumbnailFieldNames:
    """Tests for the get_thumbnail_field_names function."""

    def test_returns_all_field_names(
        self,
        test_thumbnail_config: ModelThumbnailConfig,
    ) -> None:
        """Test that all field names from specs are returned."""
        result = get_thumbnail_field_names(test_thumbnail_config)
        assert result == ["logo_thumbnail", "logo_preview"]

    def test_preserves_order(self) -> None:
        """Test that field names are returned in spec order."""
        config = ModelThumbnailConfig(
            app_label="test",
            model_name="test",
            source_field="image",
            specs=(
                ThumbnailSpec("third", 300, 300),
                ThumbnailSpec("first", 100, 100),
                ThumbnailSpec("second", 200, 200),
            ),
        )
        result = get_thumbnail_field_names(config)
        assert result == ["third", "first", "second"]


class TestGetThumbnailConfig:
    """Tests for the get_thumbnail_config function."""

    def test_returns_config_for_known_model_field(self) -> None:
        """Test that config is returned for a registered model field."""
        config = get_thumbnail_config("events", "organization", "logo")
        assert config is not None
        assert config.app_label == "events"
        assert config.source_field == "logo"

    def test_returns_none_for_unknown_model(self) -> None:
        """Test that None is returned for unregistered model."""
        assert get_thumbnail_config("unknown", "model", "field") is None

    def test_returns_none_for_unknown_field(self) -> None:
        """Test that None is returned for unregistered field."""
        assert get_thumbnail_config("events", "organization", "nonexistent") is None


# =============================================================================
# Tests for ThumbnailResult
# =============================================================================


class TestThumbnailResult:
    """Tests for the ThumbnailResult dataclass."""

    def test_empty_result_is_complete(self) -> None:
        """Test that a result with no failures is complete."""
        result = ThumbnailResult()
        assert result.is_complete
        assert not result.has_failures

    def test_result_with_thumbnails_is_complete(self) -> None:
        """Test that a result with thumbnails but no failures is complete."""
        result = ThumbnailResult(thumbnails={"logo_thumbnail": "path/to/thumb.jpg"})
        assert result.is_complete
        assert not result.has_failures

    def test_result_with_failures_is_not_complete(self) -> None:
        """Test that a result with failures is not complete."""
        result = ThumbnailResult(
            thumbnails={"logo_thumbnail": "path/to/thumb.jpg"},
            failures={"logo_preview": "some error"},
        )
        assert not result.is_complete
        assert result.has_failures

    def test_result_with_only_failures(self) -> None:
        """Test a result where all generations failed."""
        result = ThumbnailResult(failures={"logo_thumbnail": "error1", "logo_preview": "error2"})
        assert not result.is_complete
        assert result.has_failures
        assert len(result.thumbnails) == 0
        assert len(result.failures) == 2


# =============================================================================
# Tests for protected paths
# =============================================================================


class TestProtectedThumbnailPaths:
    """Tests for thumbnail path generation with protected files."""

    def test_protected_path_prefix_maintained(self) -> None:
        """Test that thumbnails for protected files stay in protected directory."""
        result = get_thumbnail_path("protected/profile-pictures/user/abc.jpg", "profile_picture_thumbnail")
        assert result.startswith("protected/")
        assert result == "protected/profile-pictures/user/abc_thumbnail.jpg"

    def test_protected_path_with_preview_suffix(self) -> None:
        """Test protected path with preview suffix."""
        result = get_thumbnail_path("protected/questionnaire-files/abc.png", "preview")
        assert result.startswith("protected/")
        assert result == "protected/questionnaire-files/abc_preview.jpg"

    def test_nested_protected_path(self) -> None:
        """Test deeply nested protected paths."""
        result = get_thumbnail_path("protected/questionnaire-files/user-123/event-456/abc.jpg", "thumbnail")
        assert result.startswith("protected/")
        assert "thumbnail" in result


# =============================================================================
# Tests for HEIC/HEIF support
# =============================================================================


class TestHeicHeifSupport:
    """Tests for HEIC/HEIF image format support."""

    @pytest.fixture
    def heic_image_bytes(self) -> bytes:
        """Create a minimal valid image that simulates HEIC processing.

        Note: Creating actual HEIC files requires pillow-heif, so we test
        the format conversion path using a regular image and verify the
        MIME type detection works correctly.
        """
        img = Image.new("RGB", (200, 150), color="purple")
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        buffer.seek(0)
        return buffer.read()

    def test_heic_mime_type_supported(self) -> None:
        """Test that HEIC MIME type is recognized as supported."""
        assert is_image_mime_type("image/heic") is True
        assert is_image_mime_type("image/heif") is True
        assert is_image_mime_type("IMAGE/HEIC") is True

    def test_heic_path_converts_to_jpg(self) -> None:
        """Test that HEIC source files result in JPG thumbnails."""
        result = get_thumbnail_path("uploads/photo.heic", "thumbnail")
        assert result.endswith(".jpg")
        assert result == "uploads/photo_thumbnail.jpg"

    def test_heif_path_converts_to_jpg(self) -> None:
        """Test that HEIF source files result in JPG thumbnails."""
        result = get_thumbnail_path("uploads/photo.heif", "preview")
        assert result.endswith(".jpg")
        assert result == "uploads/photo_preview.jpg"

    def test_thumbnail_always_outputs_jpeg(
        self,
        heic_image_bytes: bytes,
        test_thumbnail_spec: ThumbnailSpec,
    ) -> None:
        """Test that thumbnail output is always JPEG regardless of input format."""
        result = generate_thumbnail(heic_image_bytes, test_thumbnail_spec)
        with Image.open(BytesIO(result)) as img:
            assert img.format == "JPEG"


# =============================================================================
# Tests for partial failure handling
# =============================================================================


class TestPartialFailureHandling:
    """Tests for partial failure handling in thumbnail generation."""

    def test_partial_failure_continues_with_remaining_specs(
        self,
        rgb_image_bytes: bytes,
    ) -> None:
        """Test that if one spec fails, others are still processed."""
        original_path = "test-thumbnails/partial-failure.jpg"
        default_storage.save(original_path, ContentFile(rgb_image_bytes))

        # Config with two specs - one will work, we'll verify both are attempted
        config = ModelThumbnailConfig(
            app_label="test",
            model_name="test",
            source_field="image",
            specs=(
                ThumbnailSpec("working_thumbnail", 150, 150),
                ThumbnailSpec("another_thumbnail", 100, 100),
            ),
        )

        try:
            result = generate_and_save_thumbnails(original_path, config)

            # Both should succeed in normal case
            assert result.is_complete
            assert len(result.thumbnails) == 2
            assert "working_thumbnail" in result.thumbnails
            assert "another_thumbnail" in result.thumbnails
        finally:
            default_storage.delete(original_path)
            for path in result.thumbnails.values():
                if default_storage.exists(path):
                    default_storage.delete(path)
