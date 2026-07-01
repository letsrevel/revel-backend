"""Tests for Let's Revel branding on the revenue/VAT report PDF template.

Verifies that the rendered HTML carries Nata Sans, brand purple (#8C3CDD),
the let's revel. wordmark, the brand logo (in <body>), and no legacy accent
hex values.
"""

import typing as t
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.template.loader import render_to_string

# ---------------------------------------------------------------------------
# Helpers — minimal context stubs
# ---------------------------------------------------------------------------

_FONT_DIR = str(settings.BASE_DIR / "fonts")
_BRAND_LOGO = str(settings.BASE_DIR / "assets" / "brand" / "revel-logo.png")

_RATE_BUCKET = SimpleNamespace(
    vat_rate=Decimal("20.00"),
    label="20%",
    net=Decimal("100.00"),
    vat=Decimal("20.00"),
    gross=Decimal("120.00"),
    ticket_count=5,
)

_SECTION = SimpleNamespace(
    currency="EUR",
    rate_buckets=[_RATE_BUCKET],
    refunds_total=Decimal("0.00"),
    net_taxable_turnover=Decimal("100.00"),
    sold_count=5,
    refunded_count=0,
)

_SCOPE = SimpleNamespace(
    date_from=date(2024, 1, 1),
    date_to=date(2024, 1, 31),
    org=SimpleNamespace(
        name="Test Org",
        billing_name="Test Org e.U.",
        vat_id="ATU12345678",
        billing_address="Testgasse 1\n1010 Wien",
    ),
)

_DATA = SimpleNamespace(
    scope=_SCOPE,
    sections=[_SECTION],
    generated_at=datetime(2024, 2, 1, 10, 0),
)

_CTX: dict[str, t.Any] = {
    "font_dir": _FONT_DIR,
    "brand_logo": _BRAND_LOGO,
    "data": _DATA,
    "org": _SCOPE.org,
}

_LEGACY_ACCENTS = ("#667eea", "#2196F3")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_revenue_report_has_nata_sans() -> None:
    """Rendered HTML must declare the Nata Sans font family."""
    html = render_to_string("reports/revenue_vat_report.html", _CTX)
    assert "Nata Sans" in html, "Missing 'Nata Sans' font declaration"


@pytest.mark.django_db
def test_revenue_report_has_brand_purple() -> None:
    """Rendered HTML must contain brand purple #8C3CDD."""
    html = render_to_string("reports/revenue_vat_report.html", _CTX)
    assert "#8C3CDD" in html, "Missing brand purple #8C3CDD"


@pytest.mark.django_db
def test_revenue_report_has_wordmark() -> None:
    """Rendered HTML must contain the let's revel. wordmark."""
    html = render_to_string("reports/revenue_vat_report.html", _CTX)
    assert "let's revel." in html, "Missing let's revel. wordmark"


@pytest.mark.django_db
def test_revenue_report_logo_in_body() -> None:
    """Brand logo must be in <body>, not <head> (WeasyPrint hides head content)."""
    html = render_to_string("reports/revenue_vat_report.html", _CTX)
    assert "revel-logo.png" in html, "Missing brand logo reference"
    body_html = html.split("<body", 1)[1]
    assert "revel-logo.png" in body_html, (
        "Brand logo is not in <body> — WeasyPrint would render it invisible"
    )


@pytest.mark.django_db
def test_revenue_report_no_legacy_accents() -> None:
    """Rendered HTML must not contain any legacy accent hex values."""
    html = render_to_string("reports/revenue_vat_report.html", _CTX)
    for legacy in _LEGACY_ACCENTS:
        assert legacy not in html, f"Still contains legacy accent {legacy}"
