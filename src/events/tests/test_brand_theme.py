"""Unit tests for the pure brand-theme helper (#1149)."""

import pytest

from events.utils.brand_theme import (
    ACCENT_MIN_CONTRAST,
    DARK_SURFACES,
    LIGHT_SURFACES,
    PRIMARY_MIN_CONTRAST,
    BrandColors,
    contrast_ratio,
    derive_brand_theme,
    hex_to_rgb,
    hsl_to_rgb,
    is_valid_hex_color,
    relative_luminance,
    rgb_to_hex,
    rgb_to_hsl,
)

REVEL_PURPLE = "#8C3CDD"
SAMPLES = ["#8C3CDD", "#FFFFFF", "#FEFEFE", "#000000", "#0A0A0A", "#FF0000", "#FFFF00", "#00FF00", "#808080"]

RGB = tuple[int, int, int]

# Golden vectors, also consumed by the frontend as a fixture.
# (primary, accent, light (primary, fg, accent, fg), dark (primary, fg, accent, fg))
GOLDEN_VECTORS: list[
    tuple[
        str,
        str | None,
        tuple[str, str, str | None, str | None],
        tuple[str, str, str | None, str | None],
    ]
] = [
    ("#8C3CDD", "#F9B233", ("#8C3CDD", "#FFFFFF", "#C07D06", "#000000"), ("#A667E5", "#000000", "#F9B233", "#000000")),
    ("#8C3CDD", None, ("#8C3CDD", "#FFFFFF", None, None), ("#A667E5", "#000000", None, None)),
    ("#FEFEFE", None, ("#6D6D6D", "#FFFFFF", None, None), ("#FEFEFE", "#000000", None, None)),
    ("#0A0A0A", None, ("#0A0A0A", "#FFFFFF", None, None), ("#878787", "#000000", None, None)),
    ("#FF0000", None, ("#DB0000", "#FFFFFF", None, None), ("#FF2E2E", "#000000", None, None)),
    ("#FFFF00", "#00FF00", ("#707000", "#FFFFFF", "#009E00", "#000000"), ("#FFFF00", "#000000", "#00FF00", "#000000")),
    ("#808080", None, ("#6E6E6E", "#FFFFFF", None, None), ("#858585", "#000000", None, None)),
]


def _assert_meets_thresholds(colors: BrandColors, surfaces: tuple[RGB, ...]) -> None:
    for surface in surfaces:
        assert contrast_ratio(hex_to_rgb(colors.primary), surface) >= PRIMARY_MIN_CONTRAST
        if colors.accent is not None:
            assert contrast_ratio(hex_to_rgb(colors.accent), surface) >= ACCENT_MIN_CONTRAST
    assert contrast_ratio(hex_to_rgb(colors.primary), hex_to_rgb(colors.primary_foreground)) >= 4.5
    if colors.accent is not None and colors.accent_foreground is not None:
        assert contrast_ratio(hex_to_rgb(colors.accent), hex_to_rgb(colors.accent_foreground)) >= 4.5


@pytest.mark.parametrize(("primary", "accent", "light", "dark"), GOLDEN_VECTORS)
def test_golden_vectors(
    primary: str,
    accent: str | None,
    light: tuple[str, str, str | None, str | None],
    dark: tuple[str, str, str | None, str | None],
) -> None:
    theme = derive_brand_theme(primary, accent)
    assert theme is not None
    for mode, expected in ((theme.light, light), (theme.dark, dark)):
        assert (mode.primary, mode.primary_foreground, mode.accent, mode.accent_foreground) == expected


def test_conversions_round_trip() -> None:
    assert hex_to_rgb("#8c3cdd") == (140, 60, 221)
    assert rgb_to_hex((140, 60, 221)) == "#8C3CDD"
    assert hsl_to_rgb(rgb_to_hsl((140, 60, 221))) == (140, 60, 221)
    assert rgb_to_hsl((255, 0, 0)) == pytest.approx((0.0, 1.0, 0.5))


@pytest.mark.parametrize("bad", ["8C3CDD", "#8C3CD", "#8C3CDDD", "#GGGGGG", "", "red"])
def test_hex_to_rgb_rejects_invalid(bad: str) -> None:
    assert not is_valid_hex_color(bad)
    with pytest.raises(ValueError):
        hex_to_rgb(bad)


def test_contrast_ratio_extremes() -> None:
    assert relative_luminance((0, 0, 0)) == 0
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert contrast_ratio((10, 20, 30), (10, 20, 30)) == pytest.approx(1.0)


def test_none_primary_gives_no_theme() -> None:
    assert derive_brand_theme(None, None) is None
    assert derive_brand_theme(None, "#FF0000") is None
    assert derive_brand_theme("", None) is None


def test_accent_fields_none_without_accent() -> None:
    theme = derive_brand_theme(REVEL_PURPLE, None)
    assert theme is not None
    assert theme.light.accent is None and theme.light.accent_foreground is None
    assert theme.dark.accent is None and theme.dark.accent_foreground is None


def test_revel_purple_gets_lighter_dark_variant() -> None:
    theme = derive_brand_theme(REVEL_PURPLE, None)
    assert theme is not None
    original_hsl = rgb_to_hsl(hex_to_rgb(REVEL_PURPLE))
    assert rgb_to_hsl(hex_to_rgb(theme.dark.primary))[2] > original_hsl[2]
    for variant in (theme.light.primary, theme.dark.primary):
        assert rgb_to_hsl(hex_to_rgb(variant))[0] == pytest.approx(original_hsl[0], abs=3)


def test_light_mode_darkens_pale_colors() -> None:
    theme = derive_brand_theme("#FEFEFE", "#FFFF00")
    assert theme is not None
    assert rgb_to_hsl(hex_to_rgb(theme.light.primary))[2] < 0.5
    assert theme.light.accent is not None
    assert rgb_to_hsl(hex_to_rgb(theme.light.accent))[2] < rgb_to_hsl((255, 255, 0))[2]


def test_dark_mode_lightens_near_black_and_light_mode_keeps_it() -> None:
    theme = derive_brand_theme("#0a0a0a", None)
    assert theme is not None
    assert rgb_to_hsl(hex_to_rgb(theme.dark.primary))[2] > rgb_to_hsl((10, 10, 10))[2]
    assert theme.light.primary == "#0A0A0A"


@pytest.mark.parametrize("primary", SAMPLES)
@pytest.mark.parametrize("accent", [None, *SAMPLES])
def test_every_derived_color_meets_thresholds_on_both_surfaces(primary: str, accent: str | None) -> None:
    theme = derive_brand_theme(primary, accent)
    assert theme is not None
    _assert_meets_thresholds(theme.light, LIGHT_SURFACES)
    _assert_meets_thresholds(theme.dark, DARK_SURFACES)
    for mode in (theme.light, theme.dark):
        for value in (mode.primary, mode.primary_foreground, mode.accent, mode.accent_foreground):
            assert value is None or (len(value) == 7 and value == value.upper())


def test_derivation_is_deterministic() -> None:
    assert derive_brand_theme(REVEL_PURPLE, "#F9B233") == derive_brand_theme(REVEL_PURPLE, "#F9B233")


def test_surfaces_match_frontend_tokens() -> None:
    assert len(LIGHT_SURFACES) == len(DARK_SURFACES) == 2
    assert LIGHT_SURFACES[1] == (255, 255, 255)  # --card (light)
