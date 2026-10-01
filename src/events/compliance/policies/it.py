"""Italy (#1057). Tickets sold online for events held in Italy need fiscal *titoli di accesso*.

Online sales of paid *spettacoli* and *intrattenimenti* must go through an AdE-approved,
SIAE-activated issuing system; Revel is not one. Only the online channel is blocked:
offline and at-the-door payments confirmed by the organizer remain available (the
organizer issues the fiscal ticket), and so do free tickets, RSVPs and memberships.
The rule is territorial: it reaches events held in Italy, whoever the organizer is.

Docs: https://docs.letsrevel.io/compliance/eu/it/ (docs/compliance/eu/it.md).
"""

import typing as t

from django.utils.translation import gettext_lazy as _

from events.compliance.base import (
    CertifiedOnlineTicketingMixin,
    DefaultEUPolicy,
    Nexus,
    TicketComplianceField,
)
from events.compliance.registry import register

if t.TYPE_CHECKING:
    from events.models import Ticket

RESERVATION_ONLY_NOTICE = _(
    "Reservation only: this isn't a fiscal access ticket (titolo d'accesso). The organizer issues it."
)


# ponytail: online checkout is blocked for events held in Italy. The upgrade path is
# integrating a certified issuing-system partner as an ``integrations`` provider (#1057,
# medium term) and lifting this gate for organizers onboarded with it.
@register("IT")
class ItalyPolicy(CertifiedOnlineTicketingMixin, DefaultEUPolicy):
    """Italy: see the module docstring."""

    certified_system = _("a ticketing system approved by the Agenzia delle Entrate")

    def extra_ticket_fields(self, ticket: "Ticket", nexus: frozenset[Nexus]) -> list[TicketComplianceField]:
        """On priced tickets for events held here: Revel's ticket is a reservation, not the fiscal ticket."""
        from events.compliance.enforcement import tier_is_paid

        if Nexus.VENUE not in nexus or not tier_is_paid(ticket.tier):
            return []
        return [TicketComplianceField("it_reservation", str(_("Notice")), str(RESERVATION_ONLY_NOTICE))]
