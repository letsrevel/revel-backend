"""Romania (#1064). B2C invoices must be transmitted to RO e-Factura; Revel's PDF invoices are not.

The HG 846/2002 ticket mentions (series/number, organizer fiscal code, venue, date,
category, price) are all part of the EU common ticket set.

Docs: https://docs.letsrevel.io/compliance/eu/ro/ (docs/compliance/eu/ro.md).
"""

from events.compliance.base import DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("RO")
class RomaniaPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Romania: see the module docstring."""

    fiscal_system = "RO e-Factura"
