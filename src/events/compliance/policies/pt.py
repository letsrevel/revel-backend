"""Portugal (#1060). Invoices must come from AT-certified software (ATCUD, signed series).

Docs: https://docs.letsrevel.io/compliance/eu/pt/ (docs/compliance/eu/pt.md).
"""

from django.utils.translation import gettext_lazy as _

from events.compliance.base import DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("PT")
class PortugalPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Portugal: see the module docstring."""

    fiscal_system = _("certified invoicing software")
