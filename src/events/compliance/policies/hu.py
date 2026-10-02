"""Hungary (#1065). Invoices from invoicing software must reach NAV Online Számla in real time.

Organizers get a notice, next to the attendee-invoicing setting, that every paid sale
still needs a receipt or invoice from their own system, and that data on receipts not
issued by an online cash register goes to NAV from 1 September 2026 (#1092).

Docs: https://docs.letsrevel.io/compliance/eu/hu/ (docs/compliance/eu/hu.md).
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

NAV_NOTICE = _(
    "Revel can't issue attendee invoices where Hungarian rules apply: invoices from invoicing software must be "
    "reported to NAV Online Számla in real time. If this applies to you, issue a receipt (nyugta) or invoice for "
    "every paid sale from your own system. Since 1 September 2026, data on receipts not issued by an online "
    "cash register must also be reported to NAV."
)


@register("HU")
class HungaryPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Hungary: see the module docstring."""

    fiscal_system = "NAV Online Számla"
    # A seller that becomes a Hungarian taxable person for an event held here is in scope too.
    fiscal_invoicing_applies_on = ALL_NEXUS

    def organizer_notices(self, nexus: frozenset[Nexus]) -> list[ComplianceNotice]:
        """The NAV hint, wherever the invoicing block applies."""
        if not self.fiscal_invoicing_in_force(nexus):
            return []
        return [ComplianceNotice("hu_nav", NoticeTopic.ATTENDEE_INVOICING, str(NAV_NOTICE))]
