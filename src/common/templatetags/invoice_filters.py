"""Template filters for invoice/document rendering."""

from decimal import Decimal

from django import template

from common.service.invoice_utils import format_currency as _format_currency

register = template.Library()


@register.filter
def format_currency(value: Decimal | float | str, currency: str = "EUR") -> str:
    """Format a value with a currency symbol.

    Accepts Decimal, float, or string (from JSON line items).

    Usage: ``{{ fee_net|format_currency:currency }}``
    """
    return _format_currency(value, currency)


@register.filter
def format_number(value: Decimal | float | str) -> str:
    """Format a numeric value with 2 decimal places, no currency symbol.

    Usage: ``{{ amount|format_number }}``
    """
    numeric = Decimal(str(value)) if not isinstance(value, (int, float, Decimal)) else value
    return f"{numeric:,.2f}"
