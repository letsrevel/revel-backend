"""Spain (#1059). Invoicing software must be a VERI*FACTU compliant SIF; Revel is not.

VERI*FACTU applies from 2027-01-01 to corporate taxpayers and from 2027-07-01 to
everyone else. Revel cannot tell the two apart, so the block starts at the earlier
date and attendee invoicing stays allowed until then.

Until the block starts, Spanish organizers get a notice next to the attendee-invoicing
setting announcing the date (#1087). From that date the block itself explains, and the
notice goes away.

Docs: https://docs.letsrevel.io/compliance/eu/es/ (docs/compliance/eu/es.md).
"""

import datetime

from django.utils.translation import gettext_lazy as _

from events.compliance.base import ComplianceNotice, DefaultEUPolicy, FiscalizedInvoicingMixin, Nexus, NoticeTopic
from events.compliance.registry import register

# Names the date of ``SpainPolicy.fiscal_invoicing_from``; change both together.
UPCOMING_BLOCK_NOTICE = _(
    "From 1 January 2027, Revel stops issuing attendee invoices for organizers in Spain, because it can't meet "
    "Spain's invoicing-software rules (Verifactu), which start applying in 2027. If you use attendee invoicing, "
    "set up your own invoicing software before then."
)


@register("ES")
class SpainPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Spain: see the module docstring."""

    fiscal_system = "Verifactu"
    fiscal_invoicing_from = datetime.date(2027, 1, 1)

    def organizer_notices(self, nexus: frozenset[Nexus]) -> list[ComplianceNotice]:
        """The upcoming-block hint, for the sales the block will reach, until it starts."""
        if not nexus & self.fiscal_invoicing_applies_on or self.fiscal_invoicing_in_force(nexus):
            return []
        return [ComplianceNotice("es_verifactu", NoticeTopic.ATTENDEE_INVOICING, str(UPCOMING_BLOCK_NOTICE))]
