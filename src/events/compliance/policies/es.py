"""Spain (#1059), the Basque Country and Navarre (#1086).

Common territory: invoicing software must be a VERI*FACTU compliant SIF; Revel is not.
VERI*FACTU applies from 2027-01-01 to corporate taxpayers and from 2027-07-01 to
everyone else. Revel cannot tell the two apart, so the block starts at the earlier
date and attendee invoicing stays allowed until then. Until the block starts, Spanish
organizers get a notice next to the attendee-invoicing setting announcing the date
(#1087). From that date the block itself explains, and the notice goes away.

The foral territories are outside VERI*FACTU (RD 1007/2023, art. 1.3) and have their
own systems, so an organization whose city is in one resolves to its own policy
(see :func:`events.compliance.registry.resolve_org_jurisdiction`):

- **Basque Country** (``ES-PV``): TicketBAI is already mandatory in all three
  provinces (Batuz in Bizkaia), so attendee invoicing is blocked today, with no notice.
- **Navarre** (``ES-NC``): NaTicket is announced but undated. The 2027 block of the rest
  of Spain stays; only the copy changes, so it doesn't claim Verifactu.

Docs: https://docs.letsrevel.io/compliance/eu/es/ (docs/compliance/eu/es.md).
"""

import datetime
import typing as t

from django.utils.translation import gettext_lazy as _

from events.compliance.base import ComplianceNotice, DefaultEUPolicy, FiscalizedInvoicingMixin, Nexus, NoticeTopic
from events.compliance.registry import register

if t.TYPE_CHECKING:
    from django_stubs_ext import StrPromise

# Both notices name the date of ``SpainPolicy.fiscal_invoicing_from`` (Navarre inherits it); change them together.
UPCOMING_BLOCK_NOTICE = _(
    "From 1 January 2027, Revel stops issuing attendee invoices for organizers in Spain, because it can't meet "
    "Spain's invoicing-software rules (Verifactu), which start applying in 2027. If you use attendee invoicing, "
    "set up your own invoicing software before then."
)
NAVARRE_UPCOMING_BLOCK_NOTICE = _(
    "From 1 January 2027, Revel stops issuing attendee invoices for organizers in Spain, Navarre included. "
    "Navarre is bringing in its own invoicing-software rules (NaTicket), and Revel won't be connected to them. "
    "If you use attendee invoicing, set up your own invoicing software before then."
)
NAVARRE_BLOCKED_MESSAGE = _(
    "Revel doesn't issue invoices to attendees for organizers in Spain, Navarre included. Navarre is bringing in "
    "its own invoicing-software rules ({system}), and Revel isn't connected to them. Please issue invoices from "
    "your own invoicing software."
)
BASQUE_BLOCKED_MESSAGE = _(
    "Revel can't issue invoices to your attendees in the Basque Country. The law there requires invoices to go "
    "through {system} (Batuz in Bizkaia), and Revel isn't connected to it. Please issue invoices from your own "
    "TicketBAI-compliant invoicing software."
)


@register("ES")
class SpainPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Spain: see the module docstring."""

    fiscal_system = "Verifactu"
    fiscal_invoicing_from = datetime.date(2027, 1, 1)
    upcoming_block_notice_key: t.ClassVar[str] = "es_verifactu"
    upcoming_block_notice: t.ClassVar["StrPromise"] = UPCOMING_BLOCK_NOTICE

    def organizer_notices(self, nexus: frozenset[Nexus]) -> list[ComplianceNotice]:
        """The upcoming-block hint, for the sales the block will reach, until it starts."""
        if not nexus & self.fiscal_invoicing_applies_on or self.fiscal_invoicing_in_force(nexus):
            return []
        message = str(self.upcoming_block_notice)
        return [ComplianceNotice(self.upcoming_block_notice_key, NoticeTopic.ATTENDEE_INVOICING, message)]


@register("ES-NC", admin_names=("Navarre", "Navarra", "Nafarroa"))
class NavarrePolicy(SpainPolicy):
    """Navarre: Spain's 2027 block and notice, worded for NaTicket instead of Verifactu."""

    fiscal_system = "NaTicket"
    fiscal_invoicing_message = NAVARRE_BLOCKED_MESSAGE
    upcoming_block_notice_key = "es_nc_naticket"
    upcoming_block_notice = NAVARRE_UPCOMING_BLOCK_NOTICE


@register("ES-PV", admin_names=("Basque Country", "País Vasco", "Pais Vasco", "Euskadi"))
class BasqueCountryPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Basque Country: TicketBAI is in force in every province, so attendee invoicing is blocked now."""

    fiscal_system = "TicketBAI"
    fiscal_invoicing_message = BASQUE_BLOCKED_MESSAGE
