"""Greece (#1063). Retail documents must be transmitted to AADE myDATA (MARK + QR); Revel's are not.

Docs: https://docs.letsrevel.io/compliance/eu/gr/ (docs/compliance/eu/gr.md).
"""

from events.compliance.base import DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("GR")
class GreecePolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Greece (VAT prefix ``EL``): see the module docstring."""

    fiscal_system = "AADE myDATA"
