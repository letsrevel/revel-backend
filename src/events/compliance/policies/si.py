"""Slovenia (#1062). Invoices for cash-equivalent payments must be verified with FURS (ZOI/EOR).

Docs: https://docs.letsrevel.io/compliance/eu/si/ (docs/compliance/eu/si.md).
"""

from events.compliance.base import ALL_NEXUS, DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("SI")
class SloveniaPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Slovenia: see the module docstring."""

    fiscal_system = "FURS invoice verification"
    # A Slovenian supply is in scope whoever the seller is, so events held here count too.
    fiscal_invoicing_applies_on = ALL_NEXUS
