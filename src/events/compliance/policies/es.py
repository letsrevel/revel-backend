"""Spain (#1059). Invoicing software must be a VERI*FACTU / TicketBAI compliant SIF; Revel is not.

Docs: https://docs.letsrevel.io/compliance/eu/es/ (docs/compliance/eu/es.md).
"""

from events.compliance.base import DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("ES")
class SpainPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Spain: see the module docstring."""

    fiscal_system = "VERI*FACTU / TicketBAI"
