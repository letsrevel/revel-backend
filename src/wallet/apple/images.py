"""Image utilities for Apple Wallet passes.

This module handles image generation and manipulation for wallet passes,
including creating placeholder logos, resizing images, and generating icons.
"""

import colorsys
import io
from typing import Any

import structlog
from PIL import Image, ImageDraw, ImageFont

logger = structlog.get_logger(__name__)


# Image size definitions (Apple requirements)
ICON_SIZES: dict[str, tuple[int, int]] = {
    "icon.png": (29, 29),
    "icon@2x.png": (58, 58),
    "icon@3x.png": (87, 87),
}

LOGO_SIZES: dict[str, tuple[int, int]] = {
    "logo.png": (160, 50),
    "logo@2x.png": (320, 100),
    "logo@3x.png": (480, 150),
}


def generate_colored_icon(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    """Generate a simple colored square icon.

    Args:
        size: (width, height) tuple.
        color: (r, g, b) tuple.

    Returns:
        PNG image as bytes.
    """
    img = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def generate_text_logo(
    size: tuple[int, int],
    text: str,
    bg_color: tuple[int, int, int],
) -> bytes:
    """Generate a logo with text (e.g., organization initials).

    Args:
        size: (width, height) tuple.
        text: Text to display (usually 1-2 characters).
        bg_color: Background color as (r, g, b).

    Returns:
        PNG image as bytes.
    """
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw a rounded rectangle background
    margin = min(size) // 10
    draw.rounded_rectangle(
        [margin, margin, size[0] - margin, size[1] - margin],
        radius=min(size) // 5,
        fill=bg_color + (255,),
    )

    # Load font - try system fonts with fallback
    font_size = int(min(size) * 0.4)
    font = _load_font(font_size)

    # Draw text centered
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    text_x = (size[0] - text_width) // 2
    text_y = (size[1] - text_height) // 2
    draw.text((text_x, text_y), text, fill=(255, 255, 255, 255), font=font)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _load_font(font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a font with fallbacks for different platforms.

    Args:
        font_size: Desired font size in pixels.

    Returns:
        A PIL font object.
    """
    font_paths = [
        "/System/Library/Fonts/Helvetica.ttc",  # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
    ]

    for path in font_paths:
        try:
            return ImageFont.truetype(path, font_size)
        except (OSError, IOError):
            continue

    return ImageFont.load_default()


def resize_image(image_data: bytes, size: tuple[int, int]) -> bytes:
    """Resize an image to the specified size.

    Args:
        image_data: Original image as bytes.
        size: Target (width, height).

    Returns:
        Resized PNG image as bytes.
    """
    try:
        img: Image.Image = Image.open(io.BytesIO(image_data))
        img = img.resize(size, Image.Resampling.LANCZOS)

        # Keep RGBA/P modes for transparency, convert others to RGB
        if img.mode not in ("RGBA", "P", "RGB"):
            img = img.convert("RGB")

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as e:
        logger.warning("image_resize_failed", error=str(e))
        return generate_colored_icon(size, (100, 100, 100))


def resolve_cover_art(event: Any) -> bytes | None:
    """Resolve cover art image with fallback chain.

    Order: event.cover_art -> series.cover_art -> organization.cover_art

    Args:
        event: The event model with potential cover art sources.

    Returns:
        Image bytes or None if no cover art found.
    """
    sources = [
        event.cover_art,
        getattr(event.event_series, "cover_art", None) if event.event_series else None,
        event.organization.cover_art,
    ]

    for source in sources:
        if source:
            try:
                source.seek(0)
                return source.read()  # type: ignore[no-any-return]
            except Exception:
                continue

    return None


def generate_fallback_logo(organization: Any) -> bytes:
    """Generate a fallback logo based on organization ID.

    Creates a logo with organization initials on a colored background,
    where the color is derived from the organization's UUID.

    Args:
        organization: The organization model.

    Returns:
        PNG image bytes.
    """
    # Derive color from organization UUID
    hue = (organization.id.int % 360) / 360.0
    rgb = colorsys.hls_to_rgb(hue, 0.45, 0.60)
    bg_color = (int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))

    # Extract initials (first letter of first 1-2 words)
    name = organization.name or "R"
    words = name.split()[:2]
    initials = "".join(word[0].upper() for word in words if word) or "R"

    return generate_text_logo(LOGO_SIZES["logo@3x.png"], initials, bg_color)


def parse_rgb_color(rgb_string: str) -> tuple[int, int, int]:
    """Parse an RGB color string to a tuple.

    Args:
        rgb_string: Color in format "rgb(r, g, b)".

    Returns:
        Tuple of (r, g, b) integers.
    """
    import re

    match = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", rgb_string)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return (50, 50, 100)  # Default dark blue
