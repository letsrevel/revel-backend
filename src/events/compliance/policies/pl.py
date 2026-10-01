"""Poland (#1067). B2B invoices of Polish-established sellers must go through KSeF.

KSeF excludes invoices to consumers and sellers without a Polish seat or fixed
establishment, not foreign buyers, so every business buyer (domestic or foreign) is
covered. Consumers and VIES-rejected VAT IDs keep Revel's PDF invoice.

Docs: https://docs.letsrevel.io/compliance/eu/pl/ (docs/compliance/eu/pl.md).
"""

from events.compliance.base import B2BBuyerScope, B2BEInvoicingMixin, DefaultEUPolicy
from events.compliance.registry import register


@register("PL")
class PolandPolicy(B2BEInvoicingMixin, DefaultEUPolicy):
    """Poland: see the module docstring."""

    e_invoicing_network = "KSeF"
    b2b_buyer_scope = B2BBuyerScope.ANY_BUSINESS
