"""Greece (#1063). Retail documents must be transmitted to AADE myDATA (MARK + QR); Revel's are not.

Organizers get a notice, next to the attendee-invoicing setting, naming the ways to
issue Greek documents themselves (#1092).

Docs: https://docs.letsrevel.io/compliance/eu/gr/ (docs/compliance/eu/gr.md).
"""

from django.utils.translation import gettext_lazy as _

from events.compliance.base import (
    ALL_NEXUS,
    ComplianceNotice,
    DefaultEUPolicy,
    FiscalizedInvoicingMixin,
    Nexus,
    NoticeTopic,
)
from events.compliance.registry import register

MYDATA_NOTICE = _(
    "Revel can't issue attendee invoices where Greek rules apply: receipts and invoices must be transmitted to "
    "AADE's myDATA. If you must issue Greek documents, issue them from your own software, a certified "
    "e-invoicing provider or AADE's free tools (timologio, myDATAapp). Invoices to Greek businesses must be "
    "e-invoices issued through a provider or AADE's tools: since 2 March 2026 if your 2023 gross revenue was "
    "over €1 million, otherwise from 2 November 2026 (with a phase-in until 31 January 2027 if you file the "
    "declaration in time)."
)


@register("GR")
class GreecePolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Greece (VAT prefix ``EL``): see the module docstring."""

    fiscal_system = "myDATA"
    # Sellers that must issue Greek retail documents for an event held here are in scope too.
    fiscal_invoicing_applies_on = ALL_NEXUS

    def organizer_notices(self, nexus: frozenset[Nexus]) -> list[ComplianceNotice]:
        """The myDATA hint, wherever the invoicing block applies."""
        if not self.fiscal_invoicing_in_force(nexus):
            return []
        return [ComplianceNotice("gr_mydata", NoticeTopic.ATTENDEE_INVOICING, str(MYDATA_NOTICE))]
