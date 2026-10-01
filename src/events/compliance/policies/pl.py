"""Poland (#1067). Domestic B2B invoices must go through KSeF; consumer invoices are excluded.

Docs: https://docs.letsrevel.io/compliance/eu/pl/ (docs/compliance/eu/pl.md).
"""

from events.compliance.base import DefaultEUPolicy, DomesticB2BEInvoicingMixin
from events.compliance.registry import register


@register("PL")
class PolandPolicy(DomesticB2BEInvoicingMixin, DefaultEUPolicy):
    """Poland: see the module docstring."""

    e_invoicing_network = "KSeF"
