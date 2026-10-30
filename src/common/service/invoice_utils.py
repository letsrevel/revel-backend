"""Shared invoice/document utilities used across bounded contexts.

Provides currency formatting, sequential document numbering, and PDF rendering
via WeasyPrint. Domain-specific logic (aggregation, VAT rules, recipients)
stays in each app's own service module.
"""

import typing as t
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.db import models
from django.template.loader import render_to_string
from weasyprint import HTML

# Third-party issuance (#1258): Art. 220(1) of Directive 2006/112/EC lets an invoice be issued "in his
# name and on his behalf, by a third party" (AT UStR Rz 1505, 1530-1532; DE § 14 Abs. 2 S. 7 UStG). The
# one sentence printed on the attendee PDFs and carried as the EN 16931 invoice note (BT-22).
ISSUANCE_STATEMENT = "Issued by {issuer} in the name and on behalf of {seller}."


def platform_issuer_name() -> str:
    """The platform operator's legal name named as issuer on attendee documents, the brand when unset."""
    from common.models import SiteSettings

    return SiteSettings.get_solo().platform_business_name or "Revel"


def issuance_statement(issuer: str, seller: str) -> str:
    """:data:`ISSUANCE_STATEMENT` for one document; ``issuer`` falls back to :func:`platform_issuer_name`."""
    return ISSUANCE_STATEMENT.format(issuer=issuer or platform_issuer_name(), seller=seller)


CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "\u20ac",
    "USD": "$",
    "GBP": "\u00a3",
    "CHF": "CHF ",
    "DKK": "DKK ",
    "SEK": "SEK ",
    "NOK": "NOK ",
    "PLN": "PLN ",
    "CZK": "CZK ",
    "HUF": "HUF ",
    "RON": "RON ",
    "BGN": "BGN ",
}


def format_currency(value: Decimal | float | str, currency: str = "EUR") -> str:
    """Format a value with a currency symbol.

    Accepts Decimal, float, or string (from JSON line items).
    """
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")
    numeric = Decimal(str(value)) if not isinstance(value, (int, float, Decimal)) else value
    return f"{symbol}{numeric:,.2f}"


def get_next_sequential_number(
    model: type[models.Model],
    prefix: str,
    year: int,
    number_field: str,
) -> str:
    """Generate the next sequential document number for a given year.

    Format: ``{prefix}{YEAR}-{SEQUENCE:06d}``

    Must be called inside ``transaction.atomic()`` — uses ``SELECT FOR UPDATE``
    on the last record to serialize concurrent access.

    Args:
        model: The Django model class (e.g. ``PlatformFeeInvoice``).
        prefix: Number prefix including trailing dash (e.g. ``"RVL-"``).
        year: The fiscal year.
        number_field: Name of the ``CharField`` holding the number.

    Returns:
        The next sequential number string.
    """
    full_prefix = f"{prefix}{year}-"
    last = (
        model.objects.select_for_update()  # type: ignore[attr-defined]
        .filter(**{f"{number_field}__startswith": full_prefix})
        .order_by(f"-{number_field}")
        .first()
    )
    if last:
        last_seq = int(getattr(last, number_field).split("-")[-1])
        return f"{full_prefix}{last_seq + 1:06d}"
    return f"{full_prefix}{1:06d}"


def render_pdf(template_name: str, context: dict[str, t.Any], *, pdf_a3: bool = False) -> bytes:
    """Render a Django template as a PDF via WeasyPrint.

    Automatically injects ``font_dir`` and ``brand_logo`` into the context so
    every invoice/payout template gets Nata Sans and the brand logo without
    each caller repeating the path wiring.

    Args:
        template_name: Path to the Django template (e.g. ``"invoices/foo.html"``).
        context: Template context dict.
        pdf_a3: Write a PDF/A-3b document, the container Factur-X / ZUGFeRD needs (#1235).

    Returns:
        The rendered PDF as bytes.
    """
    brand_context: dict[str, t.Any] = {
        "font_dir": str(settings.BASE_DIR / "fonts"),
        "brand_logo": str(settings.BASE_DIR / "assets" / "brand" / "revel-logo.png"),
    }
    brand_context.update(context)
    html_content = render_to_string(template_name, brand_context)
    pdf_buffer = BytesIO()
    options: dict[str, t.Any] = {"pdf_variant": "pdf/a-3b"} if pdf_a3 else {}
    HTML(string=html_content).write_pdf(pdf_buffer, **options)
    return pdf_buffer.getvalue()
