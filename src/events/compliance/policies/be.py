"""Belgium (#1066). Domestic B2B invoices must be Peppol e-invoices since 1 Jan 2026; B2C PDFs stay valid.

Docs: https://docs.letsrevel.io/compliance/eu/be/ (docs/compliance/eu/be.md).
"""

from events.compliance.base import DefaultEUPolicy, DomesticB2BEInvoicingMixin
from events.compliance.registry import register


@register("BE")
class BelgiumPolicy(DomesticB2BEInvoicingMixin, DefaultEUPolicy):
    """Belgium: see the module docstring."""

    e_invoicing_network = "Peppol"
