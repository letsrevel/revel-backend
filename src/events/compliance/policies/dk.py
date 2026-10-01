"""Denmark. No restriction; one hint about digital sales registration.

Businesses whose main activity is covered (cafés, pubs and discos, pizzerias, kiosks,
restaurants) must record their sales, including online sales through third-party
portals, in their own digital sales-registration system. That duty is theirs, not a
Revel gate, so this is a notice only.

Docs: https://docs.letsrevel.io/compliance/eu/dk/ (docs/compliance/eu/dk.md).
"""

from django.utils.translation import gettext_lazy as _

from events.compliance.base import ComplianceNotice, DefaultEUPolicy, Nexus, NoticeTopic
from events.compliance.registry import register

SALES_REGISTRATION_NOTICE = _(
    "If your business must record sales digitally (for example cafés, bars and discos), record your Revel "
    "ticket and door sales there too."
)


@register("DK")
class DenmarkPolicy(DefaultEUPolicy):
    """Denmark: see the module docstring."""

    def organizer_notices(self, nexus: frozenset[Nexus]) -> list[ComplianceNotice]:
        """The sales-registration hint, for Danish organizers (the duty follows the business)."""
        if Nexus.ESTABLISHMENT not in nexus:
            return []
        return [ComplianceNotice("dk_sales_registration", NoticeTopic.OFFLINE_PAYMENT, str(SALES_REGISTRATION_NOTICE))]
