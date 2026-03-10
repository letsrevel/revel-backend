"""Template filters for invoice rendering."""

from decimal import Decimal

from django import template

from events.service.invoice_service import format_currency as _format_currency

register = template.Library()


@register.filter
def format_currency(value: Decimal | float, currency: str = "EUR") -> str:
    """Format a value with a currency symbol.

    Usage: {{ fee_net|format_currency:currency }}
    """
    return _format_currency(value, currency)
