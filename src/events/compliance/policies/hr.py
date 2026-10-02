"""Croatia (#1058). Receipts must be fiscalized (JIR/ZKI); Revel's PDF invoices are not.

Croatian organizers get a notice, next to the attendee-invoicing setting, to issue and
fiscalize invoices from their own software (#1092). Associations are obligors only when
they pay profit tax, hence "if this applies to you".

Docs: https://docs.letsrevel.io/compliance/eu/hr/ (docs/compliance/eu/hr.md).
"""

from django.utils.translation import gettext_lazy as _

from events.compliance.base import ComplianceNotice, DefaultEUPolicy, FiscalizedInvoicingMixin, Nexus, NoticeTopic
from events.compliance.registry import register

FISCALIZATION_NOTICE = _(
    "Revel can't issue your attendee invoices. In Croatia, invoices to consumers must be fiscalized in real "
    "time with the Tax Administration (Porezna uprava). If this applies to you, issue and fiscalize them from "
    "your own software."
)


@register("HR")
class CroatiaPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Croatia: see the module docstring."""

    fiscal_system = _("the Tax Administration's fiscalization system")

    def organizer_notices(self, nexus: frozenset[Nexus]) -> list[ComplianceNotice]:
        """The fiscalization hint, wherever the invoicing block applies (Croatian organizers)."""
        if not self.fiscal_invoicing_in_force(nexus):
            return []
        return [ComplianceNotice("hr_fiscalization", NoticeTopic.ATTENDEE_INVOICING, str(FISCALIZATION_NOTICE))]
