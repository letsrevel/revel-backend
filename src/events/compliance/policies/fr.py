"""France (#1061, #1233). CGI art. 290 quater ticket mentions; domestic B2B e-invoicing.

Ticket mentions (organizer, price paid or "free", a system-assigned sequential number)
are part of the EU common set every policy prints.

Domestic B2B invoices between taxable persons established in France must go through a
Plateforme Agréée: large and mid-sized companies (ETI) issue since 2026-09-01, SMEs and
micro-enterprises from 2027-09-01. Revel can't know an organizer's size, so the gate binds
every French-established organizer now (#1233). Consumers, foreign business buyers and
VIES-rejected VAT IDs keep Revel's PDF invoice. Later layers (operation journal,
SIBIL/bordereau exports, PA delivery in #1097) hook in here.

Docs: https://docs.letsrevel.io/compliance/eu/fr/ (docs/compliance/eu/fr.md).
"""

from events.compliance.base import B2BEInvoicingMixin, DefaultEUPolicy
from events.compliance.registry import register


@register("FR")
class FrancePolicy(B2BEInvoicingMixin, DefaultEUPolicy):
    """France: see the module docstring."""

    e_invoicing_network = "a Plateforme Agréée"
