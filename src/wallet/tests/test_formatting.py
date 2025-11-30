"""Tests for wallet/apple/formatting.py."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from wallet.apple.formatting import (
    REVEL_THEME,
    PassColors,
    _hsl_to_rgb_string,
    format_date_compact,
    format_date_full,
    format_iso_date,
    format_price,
    get_theme_colors,
)


class TestPassColors:
    """Tests for PassColors dataclass."""

    def test_pass_colors_is_frozen(self) -> None:
        """PassColors should be immutable."""
        colors = PassColors(background="rgb(0, 0, 0)", foreground="rgb(255, 255, 255)", label="rgb(128, 128, 128)")
        with pytest.raises(AttributeError):
            colors.background = "rgb(1, 1, 1)"  # type: ignore[misc]

    def test_pass_colors_stores_values(self) -> None:
        """PassColors should store RGB values correctly."""
        colors = PassColors(background="rgb(10, 20, 30)", foreground="rgb(40, 50, 60)", label="rgb(70, 80, 90)")
        assert colors.background == "rgb(10, 20, 30)"
        assert colors.foreground == "rgb(40, 50, 60)"
        assert colors.label == "rgb(70, 80, 90)"


class TestHslToRgbString:
    """Tests for HSL to RGB conversion."""

    def test_red_hue(self) -> None:
        """Hue 0 should produce red."""
        result = _hsl_to_rgb_string(0, 1.0, 0.5)
        assert result == "rgb(255, 0, 0)"

    def test_green_hue(self) -> None:
        """Hue 120 should produce green."""
        result = _hsl_to_rgb_string(120, 1.0, 0.5)
        assert result == "rgb(0, 255, 0)"

    def test_blue_hue(self) -> None:
        """Hue 240 should produce blue."""
        result = _hsl_to_rgb_string(240, 1.0, 0.5)
        assert result == "rgb(0, 0, 255)"

    def test_black_lightness(self) -> None:
        """Lightness 0 should produce black regardless of hue/saturation."""
        result = _hsl_to_rgb_string(180, 1.0, 0.0)
        assert result == "rgb(0, 0, 0)"

    def test_white_lightness(self) -> None:
        """Lightness 1 should produce white regardless of hue/saturation."""
        result = _hsl_to_rgb_string(180, 1.0, 1.0)
        assert result == "rgb(255, 255, 255)"

    def test_gray_saturation_zero(self) -> None:
        """Saturation 0 with mid lightness should produce gray."""
        result = _hsl_to_rgb_string(0, 0.0, 0.5)
        assert result == "rgb(127, 127, 127)"


class TestGetThemeColors:
    """Tests for get_theme_colors function."""

    def test_returns_revel_theme(self) -> None:
        """get_theme_colors should return the REVEL_THEME constant."""
        colors = get_theme_colors()
        assert colors is REVEL_THEME

    def test_theme_has_dark_background(self) -> None:
        """The theme should have a dark background (low lightness)."""
        colors = get_theme_colors()
        # Parse RGB values from background
        # "rgb(r, g, b)" format
        rgb_str = colors.background
        parts = rgb_str.replace("rgb(", "").replace(")", "").split(",")
        r, g, b = int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())
        # Dark background means low RGB values
        assert r < 50
        assert g < 50
        assert b < 50

    def test_theme_has_light_foreground(self) -> None:
        """The theme should have a light foreground (high lightness)."""
        colors = get_theme_colors()
        rgb_str = colors.foreground
        parts = rgb_str.replace("rgb(", "").replace(")", "").split(",")
        r, g, b = int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())
        # Light foreground means high RGB values
        assert r > 200
        assert g > 200
        assert b > 200


class TestFormatIsoDate:
    """Tests for format_iso_date function."""

    def test_formats_aware_datetime(self) -> None:
        """Should format aware datetime with colon in timezone."""
        dt = datetime(2025, 1, 15, 14, 30, 0, tzinfo=ZoneInfo("UTC"))
        result = format_iso_date(dt)
        assert result == "2025-01-15T14:30:00+00:00"

    def test_formats_naive_datetime(self) -> None:
        """Should make naive datetime aware and format correctly."""
        dt = datetime(2025, 6, 20, 19, 0, 0)
        result = format_iso_date(dt)
        # Should contain colon in timezone offset
        assert ":" in result[-6:]  # Last 6 chars should be like +00:00 or +02:00

    def test_positive_timezone_offset(self) -> None:
        """Should handle positive timezone offsets."""
        dt = datetime(2025, 3, 10, 10, 0, 0, tzinfo=ZoneInfo("Europe/Vienna"))
        result = format_iso_date(dt)
        # Vienna is UTC+1 in winter, UTC+2 in summer
        assert "2025-03-10T10:00:00+" in result
        # Should have colon in offset
        assert result.endswith(":00")

    def test_negative_timezone_offset(self) -> None:
        """Should handle negative timezone offsets."""
        dt = datetime(2025, 1, 15, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        result = format_iso_date(dt)
        # New York is UTC-5 in winter
        assert "2025-01-15T10:00:00-05:00" == result


class TestFormatDateCompact:
    """Tests for format_date_compact function."""

    def test_format_basic(self) -> None:
        """Should format date in compact form."""
        dt = datetime(2025, 1, 3, 19, 0, 0)
        result = format_date_compact(dt)
        assert result == "Jan 3, 2025 19:00"

    def test_format_single_digit_day(self) -> None:
        """Should not pad single-digit days."""
        dt = datetime(2025, 12, 5, 8, 30, 0)
        result = format_date_compact(dt)
        assert result == "Dec 5, 2025 08:30"

    def test_format_double_digit_day(self) -> None:
        """Should handle double-digit days."""
        dt = datetime(2025, 7, 25, 20, 45, 0)
        result = format_date_compact(dt)
        assert result == "Jul 25, 2025 20:45"


class TestFormatDateFull:
    """Tests for format_date_full function."""

    def test_format_am(self) -> None:
        """Should format morning times with AM."""
        dt = datetime(2025, 1, 3, 7, 0, 0)
        result = format_date_full(dt)
        assert result == "Jan 03, 2025 07:00 AM"

    def test_format_pm(self) -> None:
        """Should format afternoon times with PM."""
        dt = datetime(2025, 1, 3, 19, 0, 0)
        result = format_date_full(dt)
        assert result == "Jan 03, 2025 07:00 PM"

    def test_format_noon(self) -> None:
        """Should format noon correctly."""
        dt = datetime(2025, 6, 15, 12, 0, 0)
        result = format_date_full(dt)
        assert result == "Jun 15, 2025 12:00 PM"

    def test_format_midnight(self) -> None:
        """Should format midnight correctly."""
        dt = datetime(2025, 6, 15, 0, 0, 0)
        result = format_date_full(dt)
        assert result == "Jun 15, 2025 12:00 AM"


class TestFormatPrice:
    """Tests for format_price function."""

    def test_format_decimal_price(self) -> None:
        """Should format Decimal prices correctly."""
        result = format_price(Decimal("25.00"), "EUR")
        assert result == "EUR 25.00"

    def test_format_int_price(self) -> None:
        """Should format integer prices correctly."""
        result = format_price(100, "USD")
        assert result == "USD 100.00"

    def test_format_float_price(self) -> None:
        """Should format float prices correctly."""
        result = format_price(49.99, "GBP")
        assert result == "GBP 49.99"

    def test_format_zero_returns_free(self) -> None:
        """Should return 'Free' for zero price."""
        assert format_price(0, "EUR") == "Free"
        assert format_price(Decimal("0"), "USD") == "Free"
        assert format_price(0.0, "GBP") == "Free"

    def test_currency_uppercase(self) -> None:
        """Should uppercase currency code."""
        result = format_price(10, "eur")
        assert result == "EUR 10.00"

    def test_price_with_cents(self) -> None:
        """Should handle prices with cents."""
        result = format_price(Decimal("19.95"), "EUR")
        assert result == "EUR 19.95"
