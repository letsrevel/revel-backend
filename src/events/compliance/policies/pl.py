"""Poland (#1067). B2B invoices of Polish-established sellers must go through KSeF.

KSeF excludes invoices to consumers and sellers without a Polish seat or fixed
establishment, not foreign buyers, so every business buyer (domestic or foreign) is
covered. Consumers and VIES-rejected VAT IDs keep Revel's PDF invoice.

Admission sold to consumers (the cash-register duty covers sales to individuals not
running a business, VAT Act art. 111(1)) for discos, dance halls, amusement and theme
parks, and circus performances must go through the seller's own fiscal cash register
whatever the payment method (Dz.U. 2024 poz. 1902 §4 ust. 1 pkt 2 lit. k and l); other
B2C sales paid through a bank or card intermediary are exempt when each payment is
identifiable (annex poz. 42). That duty is the organizer's, and Revel can't tell a disco
from a concert, so it is a notice for every sale Poland reaches.

Docs: https://docs.letsrevel.io/compliance/eu/pl/ (docs/compliance/eu/pl.md).
"""

from django.utils.translation import gettext_lazy as _

from events.compliance.base import (
    B2BBuyerScope,
    B2BEInvoicingMixin,
    ComplianceNotice,
    DefaultEUPolicy,
    Nexus,
    NoticeTopic,
)
from events.compliance.registry import register

CASH_REGISTER_NOTICE = _(
    "Admission sold to consumers for discos, dance halls, amusement and theme parks, and circus performances "
    "must be recorded on your own fiscal cash register (kasa fiskalna), even when paid online. For other "
    "events, the online-payment exemption applies only if your records link each payment to its sale."
)


@register("PL")
class PolandPolicy(B2BEInvoicingMixin, DefaultEUPolicy):
    """Poland: see the module docstring."""

    e_invoicing_network = "KSeF"
    b2b_buyer_scope = B2BBuyerScope.ANY_BUSINESS

    def organizer_notices(self, nexus: frozenset[Nexus]) -> list[ComplianceNotice]:
        """The cash-register hint, for Polish organizers and events held in Poland."""
        return [ComplianceNotice("pl_kasa_fiskalna", NoticeTopic.TICKET_SALES, str(CASH_REGISTER_NOTICE))]
