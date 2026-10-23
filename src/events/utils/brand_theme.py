"""Organization brand-color theming (#1149).

Pure, deterministic helpers that turn an organization's chosen brand colors into a
per-mode (light/dark) palette that stays legible against the frontend's page background.

An organization stores at most two hex colors (primary, accent). One color can't reach a
usable contrast against both the light and the dark page background, so for each mode we
keep the hue and saturation and shift the HSL lightness (darker for light mode, lighter
for dark mode) in the smallest steps until the color meets its threshold against *both*
surfaces of that mode (page background and card): ``PRIMARY_MIN_CONTRAST`` (4.5:1, the
primary is used as link text, WCAG 1.4.3) and ``ACCENT_MIN_CONTRAST`` (3:1). The
foreground used on top of it is black or white, whichever contrasts more (always at least
4.5:1).
"""

import colorsys
import re

from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _
from pydantic import BaseModel, ConfigDict

HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"

hex_color_validator = RegexValidator(
    HEX_COLOR_PATTERN,
    message=_("Enter a color as a 6-digit hex value, for example #8C3CDD."),
    code="invalid_hex_color",
)

# Frontend surface tokens as (hue degrees, saturation, lightness). Keep in sync with
# revel-frontend ``src/app.css`` (``--background`` / ``--card``) and its
# ``scripts/audit-brand-themes.py``.
LIGHT_SURFACES_HSL = ((268.0, 0.60, 0.96), (0.0, 0.0, 1.0))  # --background, --card
DARK_SURFACES_HSL = ((270.0, 0.35, 0.10), (270.0, 0.30, 0.14))  # --background, --card

PRIMARY_MIN_CONTRAST = 4.5  # primary doubles as link text (WCAG 1.4.3)
ACCENT_MIN_CONTRAST = 3.0  # non-text UI contrast (WCAG 1.4.11)
_LIGHTNESS_STEP = 0.01
_BLACK = "#000000"
_WHITE = "#FFFFFF"

_HEX_RE = re.compile(HEX_COLOR_PATTERN)

RGB = tuple[int, int, int]


class BrandColors(BaseModel):
    """The derived palette for one color mode. ``accent*`` are ``None`` when no accent is set."""

    model_config = ConfigDict(frozen=True)

    primary: str
    primary_foreground: str
    accent: str | None = None
    accent_foreground: str | None = None


class BrandTheme(BaseModel):
    """Light and dark palettes derived from an organization's brand colors."""

    model_config = ConfigDict(frozen=True)

    light: BrandColors
    dark: BrandColors


def is_valid_hex_color(value: str) -> bool:
    """Whether ``value`` is a ``#RRGGBB`` color (either case)."""
    return bool(_HEX_RE.match(value))


def hex_to_rgb(value: str) -> RGB:
    """Parse ``#RRGGBB`` (any case) into an ``(r, g, b)`` tuple of 0-255 ints.

    Raises:
        ValueError: If ``value`` is not a 6-digit hex color with a leading ``#``.
    """
    if not is_valid_hex_color(value):
        raise ValueError(f"Invalid hex color: {value!r}")
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def rgb_to_hex(rgb: RGB) -> str:
    """Format an ``(r, g, b)`` tuple as an uppercase ``#RRGGBB`` string."""
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def rgb_to_hsl(rgb: RGB) -> tuple[float, float, float]:
    """Convert RGB (0-255) to HSL as (hue degrees 0-360, saturation 0-1, lightness 0-1)."""
    r, g, b = (c / 255 for c in rgb)
    h, lightness, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s, lightness


def hsl_to_rgb(hsl: tuple[float, float, float]) -> RGB:
    """Convert HSL (hue degrees, saturation 0-1, lightness 0-1) to RGB (0-255, rounded)."""
    h, s, lightness = hsl
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, lightness, s)
    return round(r * 255), round(g * 255), round(b * 255)


LIGHT_SURFACES: tuple[RGB, ...] = tuple(hsl_to_rgb(hsl) for hsl in LIGHT_SURFACES_HSL)
DARK_SURFACES: tuple[RGB, ...] = tuple(hsl_to_rgb(hsl) for hsl in DARK_SURFACES_HSL)


def relative_luminance(rgb: RGB) -> float:
    """WCAG 2.x relative luminance of an sRGB color (0 = black, 1 = white)."""

    def linearize(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linearize(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: RGB, b: RGB) -> float:
    """WCAG 2.x contrast ratio between two colors (1.0 to 21.0)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def foreground_for(rgb: RGB) -> str:
    """Return ``#000000`` or ``#FFFFFF``, whichever contrasts more with ``rgb`` (always >= 4.5:1)."""
    on_black = contrast_ratio(rgb, hex_to_rgb(_BLACK))
    on_white = contrast_ratio(rgb, hex_to_rgb(_WHITE))
    return _BLACK if on_black >= on_white else _WHITE


def ensure_contrast(color: str, surfaces: tuple[RGB, ...], minimum: float, *, lighten: bool) -> str:
    """Shift ``color``'s HSL lightness in 1% steps until it reaches ``minimum`` against every surface.

    Hue and saturation are kept. A color that already passes is returned unchanged
    (normalized to uppercase). ``lighten`` picks the direction: ``False`` darkens (light
    mode), ``True`` lightens (dark mode).
    """
    rgb = hex_to_rgb(color)

    def passes(candidate: RGB) -> bool:
        return all(contrast_ratio(candidate, surface) >= minimum for surface in surfaces)

    if passes(rgb):
        return rgb_to_hex(rgb)
    h, s, lightness = rgb_to_hsl(rgb)
    direction = 1 if lighten else -1
    # Black/white clear 4.5:1 against every surface in its direction, so the loop terminates by step 100.
    for step in range(1, int(1 / _LIGHTNESS_STEP) + 1):
        candidate = hsl_to_rgb((h, s, min(1.0, max(0.0, lightness + direction * step * _LIGHTNESS_STEP))))
        if passes(candidate):
            return rgb_to_hex(candidate)
    return rgb_to_hex(hsl_to_rgb((h, s, 1.0 if lighten else 0.0)))  # pragma: no cover


def _derive_mode(primary: str, accent: str | None, surfaces: tuple[RGB, ...], *, lighten: bool) -> BrandColors:
    primary_hex = ensure_contrast(primary, surfaces, PRIMARY_MIN_CONTRAST, lighten=lighten)
    accent_hex = ensure_contrast(accent, surfaces, ACCENT_MIN_CONTRAST, lighten=lighten) if accent else None
    return BrandColors(
        primary=primary_hex,
        primary_foreground=foreground_for(hex_to_rgb(primary_hex)),
        accent=accent_hex,
        accent_foreground=foreground_for(hex_to_rgb(accent_hex)) if accent_hex else None,
    )


def derive_brand_theme(primary: str | None, accent: str | None) -> BrandTheme | None:
    """Derive the light/dark palettes for an organization's brand colors.

    Args:
        primary: ``#RRGGBB`` primary color, or ``None`` when the organization has no branding.
        accent: ``#RRGGBB`` accent color, or ``None``.

    Returns:
        ``None`` when ``primary`` is unset (an accent alone is ignored); otherwise a
        :class:`BrandTheme` whose colors are uppercase hex and meet 4.5:1 (primary) or
        3:1 (accent) against both the page background and the card of the matching mode,
        with black/white foregrounds at 4.5:1 or better.
    """
    if not primary:
        return None
    return BrandTheme(
        light=_derive_mode(primary, accent, LIGHT_SURFACES, lighten=False),
        dark=_derive_mode(primary, accent, DARK_SURFACES, lighten=True),
    )
