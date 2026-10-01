"""Austria. No restriction; one hint about payments taken at the venue.

Card payments count as cash sales (§ 131b BAO), but Revel's online sales are exempt
(§ 6 Barumsatzverordnung 2015). Money the organizer takes at the door goes through
their own registered cash register once they pass the thresholds: their duty, not a
Revel gate, so this is a notice only.

Docs: https://docs.letsrevel.io/compliance/eu/at/ (docs/compliance/eu/at.md).
"""

from django.utils.translation import gettext_lazy as _

from events.compliance.base import ComplianceNotice, DefaultEUPolicy, Nexus, NoticeTopic
from events.compliance.registry import register

DOOR_SALES_NOTICE = _(
    "Payments you take at the door go through your own registered cash register (Registrierkasse) once you "
    "pass the legal thresholds. Revel's online sales are exempt."
)


@register("AT")
class AustriaPolicy(DefaultEUPolicy):
    """Austria: see the module docstring."""

    def organizer_notices(self, nexus: frozenset[Nexus]) -> list[ComplianceNotice]:
        """The cash-register hint, for Austrian organizers and events held in Austria."""
        return [ComplianceNotice("at_registrierkasse", NoticeTopic.OFFLINE_PAYMENT, str(DOOR_SALES_NOTICE))]
