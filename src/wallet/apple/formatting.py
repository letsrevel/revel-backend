"""Formatting utilities for Apple Wallet passes.

This module handles date formatting, color generation, and other
formatting operations for wallet pass content.
"""

import colorsys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

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


# Revel dark theme colors (HSL from frontend app.css)
# --background: 270 30% 8%
# --foreground: 270 10% 95%
# --muted-foreground: 270 10% 65%
REVEL_THEME = PassColors(
    background=_hsl_to_rgb_string(270, 0.30, 0.08),
    foreground=_hsl_to_rgb_string(270, 0.10, 0.95),
    label=_hsl_to_rgb_string(270, 0.10, 0.65),
)


def get_theme_colors() -> PassColors:
    """Get the Revel theme colors for passes.

    Returns:
        PassColors with the Revel dark theme.
    """
    return REVEL_THEME


def format_iso_date(dt: datetime) -> str:
    """Format a datetime for Apple's expected ISO 8601 format.

    Apple requires the colon in timezone offset (+00:00, not +0000).

    Args:
        dt: The datetime to format.

    Returns:
        ISO 8601 formatted string with colon in timezone.
    """
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)

    formatted = dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    # Insert colon in timezone offset: +0000 -> +00:00
    if len(formatted) >= 5 and formatted[-5] in ("+", "-"):
        formatted = formatted[:-2] + ":" + formatted[-2:]

    return formatted


def format_date_compact(dt: datetime) -> str:
    """Format a datetime compactly for pass header display.

    Omits the year (implicit from context) but keeps the time,
    which is the most useful info for an event ticket.

    Args:
        dt: The datetime to format.

    Returns:
        Formatted string like "Mar 1, 19:00".
    """
    return dt.strftime("%b %-d, %H:%M")


def format_date_full(dt: datetime) -> str:
    """Format a datetime for full display (back fields).

    Args:
        dt: The datetime to format.

    Returns:
        Formatted string like "Jan 03, 2025 07:00 PM".
    """
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
