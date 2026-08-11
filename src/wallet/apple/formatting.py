"""Formatting utilities for Apple Wallet passes.

This module handles date formatting, color generation, and other
formatting operations for wallet pass content.
"""

import colorsys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone


@dataclass(frozen=True)
class PassColors:
    """Colors for an Apple Wallet pass in RGB format."""

    background: str  # Format: "rgb(r, g, b)"
    foreground: str
    label: str


def _hsl_to_rgb_string(hue: float, saturation: float, lightness: float) -> str:
    """Convert HSL values to an RGB color string.

    Args:
        hue: Hue in degrees (0-360).
        saturation: Saturation (0-1).
        lightness: Lightness (0-1).

    Returns:
        Color string in format "rgb(r, g, b)".
    """
    # colorsys uses HLS order (hue, lightness, saturation)
    rgb = colorsys.hls_to_rgb(hue / 360, lightness, saturation)
    return f"rgb({int(rgb[0] * 255)}, {int(rgb[1] * 255)}, {int(rgb[2] * 255)})"


# Brand tokens. The two primaries use the digital style guide's exact values
# (Revel Digital Brand Styleguide, "Colours"): the hexes are the contract —
# HSL->RGB truncation would land one unit off (#8C3BDC != #8C3CDD), and the
# guide PDF's printed RGB for Light Crimson (230, 52, 42) is itself a typo
# for hex E6332A. The label keeps the frontend's lavender-paper HSL token
# (--background light: 268 60% 96%, rendering as #F4EEFA — one RGB unit
# off the ticket PDF's paper #F3EFFA; the HSL token is the contract there).
_HEARTY_PURPLE_RGB = (140, 60, 221)  # #8C3CDD, frontend --logo-from / --poster-purple
_LIGHT_CRIMSON_RGB = (230, 51, 42)  # #E6332A, frontend --logo-to / --poster-crimson
_LAVENDER_PAPER_HSL = (268, 0.60, 0.96)


def _rgb_string(rgb: tuple[int, int, int]) -> str:
    """Format an RGB tuple as Apple's expected "rgb(r, g, b)" string.

    Args:
        rgb: (r, g, b) channel values, 0-255.

    Returns:
        Color string in format "rgb(r, g, b)".
    """
    return f"rgb({rgb[0]}, {rgb[1]}, {rgb[2]})"


# Revel brand theme (2026 gradient rebrand): Hearty Purple panel (white text
# measures ~5.5:1, AA pass), lavender-paper labels. The Apple rail layers the
# vertical purple->crimson gradient background.png on top of this
# backgroundColor; the Google rail can only render the solid hex.
REVEL_THEME = PassColors(
    background=_rgb_string(_HEARTY_PURPLE_RGB),
    foreground="rgb(255, 255, 255)",
    label=_hsl_to_rgb_string(*_LAVENDER_PAPER_HSL),
)


def get_theme_colors() -> PassColors:
    """Get the Revel theme colors for passes.

    Returns:
        PassColors with the Revel brand theme.
    """
    return REVEL_THEME


def get_gradient_rgb() -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Get the brand gradient endpoints for image generation.

    Returns:
        (start, end) RGB tuples: Hearty Purple -> Light Crimson.
    """
    return _HEARTY_PURPLE_RGB, _LIGHT_CRIMSON_RGB


def get_theme_hex_background() -> str:
    """Get the Hearty Purple background as hex, for the Google Wallet rail.

    Returns:
        Uppercase #RRGGBB string for the same RGB token as
        ``REVEL_THEME.background``.
    """
    return "#{:02X}{:02X}{:02X}".format(*_HEARTY_PURPLE_RGB)


def format_iso_date(dt: datetime, tz: ZoneInfo | None = None) -> str:
    """Format a datetime for Apple's expected ISO 8601 format.

    Apple requires the colon in timezone offset (+00:00, not +0000).

    Args:
        dt: The datetime to format.
        tz: Optional timezone to convert to before formatting.

    Returns:
        ISO 8601 formatted string with colon in timezone.
    """
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)

    if tz:
        dt = dt.astimezone(tz)

    formatted = dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    # Insert colon in timezone offset: +0000 -> +00:00
    if len(formatted) >= 5 and formatted[-5] in ("+", "-"):
        formatted = formatted[:-2] + ":" + formatted[-2:]

    return formatted


def format_date_compact(dt: datetime, tz: ZoneInfo | None = None) -> str:
    """Format a datetime compactly for pass header display.

    Omits the year (implicit from context) but keeps the time,
    which is the most useful info for an event ticket.

    Args:
        dt: The datetime to format.
        tz: Optional timezone to convert to before formatting.

    Returns:
        Formatted string like "Mar 1, 19:00".
    """
    if tz:
        dt = dt.astimezone(tz)
    return dt.strftime("%b %-d, %H:%M")


def format_date_full(dt: datetime, tz: ZoneInfo | None = None) -> str:
    """Format a datetime for full display (back fields).

    Args:
        dt: The datetime to format.
        tz: Optional timezone to convert to before formatting.

    Returns:
        Formatted string like "Jan 03, 2025 07:00 PM".
    """
    if tz:
        dt = dt.astimezone(tz)
    return dt.strftime("%b %d, %Y %I:%M %p")


def format_price(price: Decimal | int | float, currency: str) -> str:
    """Format a price for display on the pass.

    Args:
        price: The price amount.
        currency: Currency code (e.g., "EUR", "USD").

    Returns:
        Formatted string like "EUR 25.00" or "Free".
    """
    if price == 0:
        return "Free"
    return f"{currency.upper()} {float(price):.2f}"
