"""Tests for Let's Revel invoice/credit-note/payout PDF branding.

Verifies that render_pdf injects font_dir + brand_logo into template context,
that all invoice templates render with Nata Sans, brand purple (#8C3CDD), and
the let's revel. wordmark, and that the brand logo asset is present on disk.
"""

import typing as t
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.template.loader import render_to_string

from common.service.invoice_utils import render_pdf

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BRAND_LOGO = Path(settings.BASE_DIR) / "assets" / "brand" / "revel-logo.png"

# ---------------------------------------------------------------------------
# Asset existence
# ---------------------------------------------------------------------------


def test_brand_logo_exists_and_correct_size() -> None:
    """The committed brand logo PNG must exist and be ~600 px wide."""
    from PIL import Image

    assert BRAND_LOGO.exists(), f"brand logo not found at {BRAND_LOGO}"
    img = Image.open(BRAND_LOGO)
    assert img.width == 600, f"expected 600px wide, got {img.width}"
    assert img.mode == "RGB"


# ---------------------------------------------------------------------------
# render_pdf DRY injection
# ---------------------------------------------------------------------------


def test_render_pdf_injects_font_dir_and_brand_logo(monkeypatch: pytest.MonkeyPatch) -> None:
    """render_pdf must merge font_dir and brand_logo into context automatically."""
    captured: dict[str, t.Any] = {}

    def fake_render(template_name: str, context: dict[str, t.Any]) -> str:
        captured.update(context)
        return "<html><body>test</body></html>"

    monkeypatch.setattr("common.service.invoice_utils.render_to_string", fake_render)

    # WeasyPrint is also called — monkeypatch it
    class FakeHTML:
        def __init__(self, string: str) -> None:
            pass

        def write_pdf(self, buf: t.Any) -> None:
            buf.write(b"%PDF-fake")

    monkeypatch.setattr("common.service.invoice_utils.HTML", FakeHTML)

    render_pdf("invoices/attendee_invoice.html", {"foo": "bar"})

    assert "font_dir" in captured, "render_pdf did not inject font_dir"
    assert "brand_logo" in captured, "render_pdf did not inject brand_logo"
    assert captured["font_dir"] == str(settings.BASE_DIR / "fonts")
    assert captured["brand_logo"] == str(settings.BASE_DIR / "assets" / "brand" / "revel-logo.png")


# ---------------------------------------------------------------------------
# Template HTML-level assertions
# ---------------------------------------------------------------------------

# Minimal but sufficient context for each template so render_to_string doesn't crash.
# We only need the HTML output — we're not exercising database logic here.

_COMMON_CTX: dict[str, t.Any] = {
    "font_dir": str(settings.BASE_DIR / "fonts"),
    "brand_logo": str(settings.BASE_DIR / "assets" / "brand" / "revel-logo.png"),
}

_INVOICE_CTX: dict[str, t.Any] = {
    **_COMMON_CTX,
    "invoice": type(
        "_Invoice",
        (),
        {
            "invoice_number": "RVL-2024-000001",
            "seller_name": "Let's Revel e.U.",
            "seller_address": "Testgasse 1, 1010 Wien",
            "seller_vat_id": "ATU12345678",
            "buyer_name": "Test Buyer",
            "buyer_address": "Buyer St 2",
            "buyer_vat_id": "",
            "buyer_email": "buyer@example.com",
            "issued_at": None,
            "currency": "EUR",
            "vat_rate": 20,
            "total_net": "100.00",
            "total_vat": "20.00",
            "total_gross": "120.00",
            "discount_amount_total": None,
            "line_items": [],
            "reverse_charge": False,
        },
    )(),
    "is_draft": False,
    "is_export": False,
}

_CREDIT_NOTE_CTX: dict[str, t.Any] = {
    **_COMMON_CTX,
    "invoice": _INVOICE_CTX["invoice"],
    "credit_note": type(
        "_CreditNote",
        (),
        {
            "credit_note_number": "CN-2024-000001",
            "issued_at": None,
            "amount_net": "100.00",
            "amount_vat": "20.00",
            "amount_gross": "120.00",
            "line_items": [],
        },
    )(),
    "is_export": False,
}

_PLATFORM_FEE_CTX: dict[str, t.Any] = {
    **_COMMON_CTX,
    "platform_business_name": "Let's Revel e.U.",
    "platform_business_address": "Testgasse 1, 1010 Wien",
    "platform_vat_id": "ATU12345678",
    "invoice_number": "PFI-2024-000001",
    "issued_date": "2024-01-01",
    "period_start": "2024-01-01",
    "period_end": "2024-01-31",
    "period_label": "January 2024",
    "currency": "EUR",
    "org_name": "Test Org",
    "org_address": "Org St 1",
    "org_vat_id": "",
    "total_tickets": 10,
    "total_ticket_revenue": "1000.00",
    "ticket_fee_net": "30.00",
    "total_subscription_payments": 4,
    "total_subscription_revenue": "400.00",
    "membership_fee_net": "20.00",
    "fee_net": "50.00",
    "fee_vat": "10.00",
    "fee_gross": "60.00",
    "fee_vat_rate": 20,
    "reverse_charge": False,
}

_REFERRAL_CTX: dict[str, t.Any] = {
    **_COMMON_CTX,
    "document_title": "Payout Statement",
    "document_number": "REF-2024-000001",
    "platform_business_name": "Let's Revel e.U.",
    "platform_business_address": "Testgasse 1, 1010 Wien",
    "platform_vat_id": "ATU12345678",
    "issued_date": "2024-01-01",
    "period_start": "2024-01-01",
    "period_end": "2024-01-31",
    "period_label": "January 2024",
    "currency": "EUR",
    "referrer_name": "Test Referrer",
    "referrer_address": "Referrer St 1",
    "referrer_vat_id": "",
    "amount_net": "50.00",
    "amount_vat": "10.00",
    "amount_gross": "60.00",
    "vat_rate": 20,
    "is_b2b": False,
    "reverse_charge": False,
}

_TEMPLATES_AND_CONTEXTS: list[tuple[str, dict[str, t.Any]]] = [
    ("invoices/attendee_invoice.html", _INVOICE_CTX),
    ("invoices/attendee_credit_note.html", _CREDIT_NOTE_CTX),
    ("invoices/platform_fee_invoice.html", _PLATFORM_FEE_CTX),
    ("invoices/referral_payout_statement.html", _REFERRAL_CTX),
]

_LEGACY_ACCENTS = ("#667eea", "#764ba2")


@pytest.mark.parametrize("tpl,ctx", _TEMPLATES_AND_CONTEXTS, ids=[t_[0] for t_ in _TEMPLATES_AND_CONTEXTS])
def test_invoice_template_branding(tpl: str, ctx: dict[str, t.Any]) -> None:
    """Each invoice template must carry Nata Sans, brand purple, and the wordmark."""
    html = render_to_string(tpl, ctx)

    assert "Nata Sans" in html, f"{tpl}: missing 'Nata Sans' in output"
    assert "#8C3CDD" in html, f"{tpl}: missing brand purple #8C3CDD"
    assert "let's revel." in html, f"{tpl}: missing 'let's revel.' wordmark"
    assert "revel-logo.png" in html, f"{tpl}: missing brand logo reference"

    # Prove the logo is in <body>, not hidden in <head> (WeasyPrint won't render head content).
    body_html = html.split("<body", 1)[1]
    assert "revel-logo.png" in body_html, f"{tpl}: brand logo is not in <body> — WeasyPrint would render it invisible"

    for legacy in _LEGACY_ACCENTS:
        assert legacy not in html, f"{tpl}: still contains legacy accent {legacy}"


# ---------------------------------------------------------------------------
# render_pdf smoke test — produces non-empty PDF bytes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Discount totals box (#909): the column must foot, discount is informational
# ---------------------------------------------------------------------------


def _invoice_ctx_with_discount(discount_amount_total: Decimal | None) -> dict[str, t.Any]:
    """A copy of `_INVOICE_CTX` with `discount_amount_total` overridden.

    Uses a real ``Decimal`` (or ``None``), never a string: template truthiness for
    ``Decimal("0.00")`` is ``False`` (matching the production model field), while
    the string ``"0.00"`` is truthy and would give this test a false positive.
    """
    invoice = _INVOICE_CTX["invoice"]
    attrs = {k: v for k, v in vars(type(invoice)).items() if not k.startswith("__")}
    invoice_with_discount = type("_Invoice", (), {**attrs, "discount_amount_total": discount_amount_total})()
    return {**_INVOICE_CTX, "invoice": invoice_with_discount}


def test_discounted_invoice_totals_column_foots_and_shows_informational_discount_line() -> None:
    """The totals column must not double-subtract the discount (#909).

    total_gross is total_net + total_vat (the amount actually charged, already
    net of the discount) -- so a totals-row that subtracts the discount again
    breaks footing. The discount must render only as an informational note
    below the Total row, not as an arithmetic row between VAT and Total.
    """
    ctx = _invoice_ctx_with_discount(Decimal("10.00"))

    html = render_to_string("invoices/attendee_invoice.html", ctx)

    # No totals-row discount row between VAT and Total.
    assert "<span>Discount</span>" not in html

    # An informational discount line is present, distinct from the per-line note.
    assert "Includes discount of" in html
    assert "10.00" in html.split("Includes discount of", 1)[1][:50]


def test_zero_discount_invoice_renders_no_informational_discount_line() -> None:
    """An invoice with no discount must not render the informational note."""
    ctx = _invoice_ctx_with_discount(Decimal("0.00"))

    html = render_to_string("invoices/attendee_invoice.html", ctx)

    assert "Includes discount of" not in html


@pytest.mark.django_db
def test_render_pdf_produces_bytes() -> None:
    """render_pdf on platform_fee_invoice must return non-empty PDF bytes."""
    result = render_pdf("invoices/platform_fee_invoice.html", _PLATFORM_FEE_CTX)
    assert isinstance(result, bytes)
    assert len(result) > 0
    # PDF magic bytes
    assert result[:4] == b"%PDF"
