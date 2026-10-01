"""Italy (#1057). Paid admission needs fiscal *titoli di accesso*.

Paid *spettacoli* and *intrattenimenti* need tickets from an AdE-approved, SIAE-activated
issuing system; Revel is not one. Free tickets, RSVPs and memberships stay available.

Docs: https://docs.letsrevel.io/compliance/eu/it/ (docs/compliance/eu/it.md).
"""

from events.compliance.base import CertifiedTicketingMixin, DefaultEUPolicy
from events.compliance.registry import register


# ponytail: paid ticketing is blocked outright. The upgrade path is integrating a certified
# issuing-system partner as an ``integrations`` provider (#1057, medium term) and lifting
# this gate for organizers onboarded with it.
@register("IT")
class ItalyPolicy(CertifiedTicketingMixin, DefaultEUPolicy):
    """Italy: see the module docstring."""

    certified_system = "an AdE-approved fiscal ticketing system (SIAE)"
