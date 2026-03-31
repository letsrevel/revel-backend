"""Shared invoice/document utilities used across bounded contexts.

Provides currency formatting, sequential document numbering, and PDF rendering
via WeasyPrint. Domain-specific logic (aggregation, VAT rules, recipients)
stays in each app's own service module.
"""

import typing as t
from decimal import Decimal
from io import BytesIO

from django.db import models
from django.template.loader import render_to_string
from weasyprint import HTML

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


def render_pdf(template_name: str, context: dict[str, t.Any]) -> bytes:
    """Render a Django template as a PDF via WeasyPrint.

    Args:
        template_name: Path to the Django template (e.g. ``"invoices/foo.html"``).
        context: Template context dict.

    Returns:
        The rendered PDF as bytes.
    """
    html_content = render_to_string(template_name, context)
    pdf_buffer = BytesIO()
    HTML(string=html_content).write_pdf(pdf_buffer)
    return pdf_buffer.getvalue()
