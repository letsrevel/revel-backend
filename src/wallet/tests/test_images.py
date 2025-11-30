"""Tests for wallet/apple/images.py."""

import io
from unittest.mock import MagicMock
from uuid import UUID

from PIL import Image

from wallet.apple.images import (
    ICON_SIZES,
    LOGO_SIZES,
    generate_colored_icon,
    generate_fallback_logo,
    generate_text_logo,
    parse_rgb_color,
    resize_image,
    resolve_cover_art,
)


class TestIconSizes:
    """Tests for ICON_SIZES constant."""

    def test_contains_all_variants(self) -> None:
        """Should contain 1x, 2x, 3x variants."""
        assert "icon.png" in ICON_SIZES
        assert "icon@2x.png" in ICON_SIZES
        assert "icon@3x.png" in ICON_SIZES

    def test_sizes_are_squares(self) -> None:
        """All icon sizes should be square."""
        for filename, size in ICON_SIZES.items():
            assert size[0] == size[1], f"{filename} should be square"

    def test_sizes_scale_correctly(self) -> None:
        """Sizes should scale as 1x, 2x, 3x."""
        base_size = ICON_SIZES["icon.png"][0]
        assert ICON_SIZES["icon@2x.png"][0] == base_size * 2
        assert ICON_SIZES["icon@3x.png"][0] == base_size * 3


class TestLogoSizes:
    """Tests for LOGO_SIZES constant."""

    def test_contains_all_variants(self) -> None:
        """Should contain 1x, 2x, 3x variants."""
        assert "logo.png" in LOGO_SIZES
        assert "logo@2x.png" in LOGO_SIZES
        assert "logo@3x.png" in LOGO_SIZES

    def test_sizes_scale_correctly(self) -> None:
        """Sizes should scale as 1x, 2x, 3x."""
        base_width, base_height = LOGO_SIZES["logo.png"]
        assert LOGO_SIZES["logo@2x.png"] == (base_width * 2, base_height * 2)
        assert LOGO_SIZES["logo@3x.png"] == (base_width * 3, base_height * 3)


class TestGenerateColoredIcon:
    """Tests for generate_colored_icon function."""

    def test_generates_valid_png(self) -> None:
        """Should generate valid PNG bytes."""
        result = generate_colored_icon((29, 29), (255, 0, 0))
        # Check PNG signature
        assert result[:8] == b"\x89PNG\r\n\x1a\n"

    def test_generates_correct_size(self) -> None:
        """Should generate image with correct dimensions."""
        result = generate_colored_icon((58, 58), (0, 255, 0))
        img = Image.open(io.BytesIO(result))
        assert img.size == (58, 58)

    def test_generates_correct_color(self) -> None:
        """Should generate image with specified color."""
        result = generate_colored_icon((10, 10), (255, 128, 64))
        img = Image.open(io.BytesIO(result))
        # Check center pixel color
        pixel = img.getpixel((5, 5))
        assert pixel == (255, 128, 64)

    def test_generates_rgb_mode(self) -> None:
        """Should generate RGB mode image."""
        result = generate_colored_icon((29, 29), (100, 100, 100))
        img = Image.open(io.BytesIO(result))
        assert img.mode == "RGB"


class TestGenerateTextLogo:
    """Tests for generate_text_logo function."""

    def test_generates_valid_png(self) -> None:
        """Should generate valid PNG bytes."""
        result = generate_text_logo((160, 50), "AB", (100, 50, 150))
        assert result[:8] == b"\x89PNG\r\n\x1a\n"

    def test_generates_correct_size(self) -> None:
        """Should generate image with correct dimensions."""
        result = generate_text_logo((320, 100), "XY", (50, 100, 150))
        img = Image.open(io.BytesIO(result))
        assert img.size == (320, 100)

    def test_generates_rgba_mode(self) -> None:
        """Should generate RGBA mode image (for transparency)."""
        result = generate_text_logo((160, 50), "AB", (100, 100, 100))
        img = Image.open(io.BytesIO(result))
        assert img.mode == "RGBA"

    def test_single_character_text(self) -> None:
        """Should handle single character text."""
        result = generate_text_logo((160, 50), "R", (100, 100, 100))
        assert len(result) > 0
        img = Image.open(io.BytesIO(result))
        assert img.size == (160, 50)

    def test_empty_text(self) -> None:
        """Should handle empty text gracefully."""
        result = generate_text_logo((160, 50), "", (100, 100, 100))
        assert len(result) > 0


class TestResizeImage:
    """Tests for resize_image function."""

    def test_resizes_correctly(self, sample_logo_bytes: bytes) -> None:
        """Should resize image to target dimensions."""
        result = resize_image(sample_logo_bytes, (50, 50))
        img = Image.open(io.BytesIO(result))
        assert img.size == (50, 50)

    def test_returns_png_format(self, sample_logo_bytes: bytes) -> None:
        """Should return PNG format."""
        result = resize_image(sample_logo_bytes, (80, 80))
        assert result[:8] == b"\x89PNG\r\n\x1a\n"

    def test_handles_upscaling(self, sample_logo_bytes: bytes) -> None:
        """Should handle upscaling images."""
        # sample_logo_bytes is 100x100
        result = resize_image(sample_logo_bytes, (200, 200))
        img = Image.open(io.BytesIO(result))
        assert img.size == (200, 200)

    def test_handles_invalid_image_gracefully(self) -> None:
        """Should return fallback image for invalid data."""
        result = resize_image(b"not an image", (50, 50))
        img = Image.open(io.BytesIO(result))
        # Should return a gray fallback
        assert img.size == (50, 50)

    def test_preserves_rgba_transparency(self) -> None:
        """Should preserve RGBA transparency."""
        # Create an RGBA image with transparency
        img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        input_bytes = buffer.getvalue()

        result = resize_image(input_bytes, (50, 50))
        result_img = Image.open(io.BytesIO(result))
        assert result_img.mode in ("RGBA", "P")  # P mode also supports transparency


class TestResolveCoverArt:
    """Tests for resolve_cover_art function."""

    def test_returns_event_cover_art_first(self, sample_logo_bytes: bytes) -> None:
        """Should prioritize event cover_art."""
        mock_event = MagicMock()
        mock_event.cover_art = io.BytesIO(sample_logo_bytes)
        mock_event.event_series = None
        mock_event.organization = MagicMock()
        mock_event.organization.cover_art = None

        result = resolve_cover_art(mock_event)
        assert result == sample_logo_bytes

    def test_falls_back_to_series_cover_art(self, sample_logo_bytes: bytes) -> None:
        """Should fall back to series cover_art when event has none."""
        mock_event = MagicMock()
        mock_event.cover_art = None
        mock_event.event_series = MagicMock()
        mock_event.event_series.cover_art = io.BytesIO(sample_logo_bytes)
        mock_event.organization = MagicMock()
        mock_event.organization.cover_art = None

        result = resolve_cover_art(mock_event)
        assert result == sample_logo_bytes

    def test_falls_back_to_organization_cover_art(self, sample_logo_bytes: bytes) -> None:
        """Should fall back to organization cover_art when others are unavailable."""
        mock_event = MagicMock()
        mock_event.cover_art = None
        mock_event.event_series = None
        mock_event.organization = MagicMock()
        mock_event.organization.cover_art = io.BytesIO(sample_logo_bytes)

        result = resolve_cover_art(mock_event)
        assert result == sample_logo_bytes

    def test_returns_none_when_no_cover_art(self) -> None:
        """Should return None when no cover art is available."""
        mock_event = MagicMock()
        mock_event.cover_art = None
        mock_event.event_series = None
        mock_event.organization = MagicMock()
        mock_event.organization.cover_art = None

        result = resolve_cover_art(mock_event)
        assert result is None

    def test_handles_event_series_none(self) -> None:
        """Should handle event with no event_series."""
        mock_event = MagicMock()
        mock_event.cover_art = None
        mock_event.event_series = None
        mock_event.organization = MagicMock()
        mock_event.organization.cover_art = None

        result = resolve_cover_art(mock_event)
        assert result is None

    def test_handles_read_error_gracefully(self) -> None:
        """Should handle read errors and try next source."""
        mock_event = MagicMock()
        mock_event.cover_art = MagicMock()
        mock_event.cover_art.seek.side_effect = Exception("Read error")
        mock_event.event_series = None
        mock_event.organization = MagicMock()
        mock_event.organization.cover_art = None

        result = resolve_cover_art(mock_event)
        assert result is None


class TestGenerateFallbackLogo:
    """Tests for generate_fallback_logo function."""

    def test_generates_valid_png(self, mock_organization: MagicMock) -> None:
        """Should generate valid PNG bytes."""
        result = generate_fallback_logo(mock_organization)
        assert result[:8] == b"\x89PNG\r\n\x1a\n"

    def test_generates_logo_at_3x_size(self, mock_organization: MagicMock) -> None:
        """Should generate at logo@3x.png size."""
        result = generate_fallback_logo(mock_organization)
        img = Image.open(io.BytesIO(result))
        assert img.size == LOGO_SIZES["logo@3x.png"]

    def test_uses_organization_initials(self, mock_organization: MagicMock) -> None:
        """Should generate logo for organization name."""
        # "Test Organization" -> "TO"
        result = generate_fallback_logo(mock_organization)
        assert len(result) > 0

    def test_handles_single_word_name(self) -> None:
        """Should handle single word organization name."""
        org = MagicMock()
        org.id = UUID("12345678-1234-5678-1234-567812345678")
        org.name = "Acme"

        result = generate_fallback_logo(org)
        assert len(result) > 0

    def test_handles_empty_name(self) -> None:
        """Should handle empty organization name with fallback."""
        org = MagicMock()
        org.id = UUID("12345678-1234-5678-1234-567812345678")
        org.name = ""

        result = generate_fallback_logo(org)
        assert len(result) > 0

    def test_handles_none_name(self) -> None:
        """Should handle None organization name."""
        org = MagicMock()
        org.id = UUID("12345678-1234-5678-1234-567812345678")
        org.name = None

        result = generate_fallback_logo(org)
        assert len(result) > 0

    def test_color_derived_from_uuid(self) -> None:
        """Different UUIDs should produce different colors."""
        org1 = MagicMock()
        org1.id = UUID("11111111-1111-1111-1111-111111111111")
        org1.name = "Test"

        org2 = MagicMock()
        org2.id = UUID("99999999-9999-9999-9999-999999999999")
        org2.name = "Test"

        result1 = generate_fallback_logo(org1)
        result2 = generate_fallback_logo(org2)

        # Images should be different due to different colors
        # (same text but different backgrounds)
        assert result1 != result2


class TestParseRgbColor:
    """Tests for parse_rgb_color function."""

    def test_parses_valid_rgb_string(self) -> None:
        """Should parse valid RGB string."""
        result = parse_rgb_color("rgb(255, 128, 64)")
        assert result == (255, 128, 64)

    def test_parses_rgb_without_spaces(self) -> None:
        """Should parse RGB string without spaces."""
        result = parse_rgb_color("rgb(100,50,25)")
        assert result == (100, 50, 25)

    def test_parses_rgb_with_extra_spaces(self) -> None:
        """Should parse RGB string with extra spaces."""
        result = parse_rgb_color("rgb(100,  50,  25)")
        assert result == (100, 50, 25)

    def test_returns_default_for_invalid_string(self) -> None:
        """Should return default color for invalid input."""
        result = parse_rgb_color("invalid")
        assert result == (50, 50, 100)

    def test_returns_default_for_empty_string(self) -> None:
        """Should return default color for empty string."""
        result = parse_rgb_color("")
        assert result == (50, 50, 100)

    def test_returns_default_for_hex_color(self) -> None:
        """Should return default color for hex format."""
        result = parse_rgb_color("#ff8040")
        assert result == (50, 50, 100)

    def test_parses_black(self) -> None:
        """Should parse black color."""
        result = parse_rgb_color("rgb(0, 0, 0)")
        assert result == (0, 0, 0)

    def test_parses_white(self) -> None:
        """Should parse white color."""
        result = parse_rgb_color("rgb(255, 255, 255)")
        assert result == (255, 255, 255)
