"""Hungary (#1065). Invoices from invoicing software must reach NAV Online Számla in real time.

Docs: https://docs.letsrevel.io/compliance/eu/hu/ (docs/compliance/eu/hu.md).
"""

from events.compliance.base import ALL_NEXUS, DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("HU")
class HungaryPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Hungary: see the module docstring."""

    fiscal_system = "NAV Online Számla"
    # A seller that becomes a Hungarian taxable person for an event held here is in scope too.
    fiscal_invoicing_applies_on = ALL_NEXUS
