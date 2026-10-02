"""Slovenia (#1062). Invoices for cash-equivalent payments must be verified with FURS (ZOI/EOR).

Organizers get a notice, next to the attendee-invoicing setting, to issue verified
invoices from their own software, also with invoicing turned off: FURS treats card
payments, and online payments paid out in batches (as Stripe usually does), as cash,
so every paid sale still needs one when an invoice is owed (#1092).

Docs: https://docs.letsrevel.io/compliance/eu/si/ (docs/compliance/eu/si.md).
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

FURS_NOTICE = _(
    "Revel can't issue attendee invoices where Slovenian rules apply: invoices paid by card or online must be "
    "verified with FURS in real time. FURS treats card payments, and online payments paid out to you in batches "
    "(as Stripe usually does), as cash payments. If you must issue invoices, issue a FURS-verified invoice for "
    "every paid sale from your own software, even with attendee invoicing turned off."
)


@register("SI")
class SloveniaPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Slovenia: see the module docstring."""

    fiscal_system = _("FURS invoice verification")
    # A Slovenian supply is in scope whoever the seller is, so events held here count too.
    fiscal_invoicing_applies_on = ALL_NEXUS

    def organizer_notices(self, nexus: frozenset[Nexus]) -> list[ComplianceNotice]:
        """The FURS hint, wherever the invoicing block applies."""
        if not self.fiscal_invoicing_in_force(nexus):
            return []
        return [ComplianceNotice("si_furs", NoticeTopic.ATTENDEE_INVOICING, str(FURS_NOTICE))]
