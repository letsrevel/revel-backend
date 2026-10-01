"""Croatia (#1058). Receipts must be fiscalized (JIR/ZKI); Revel's PDF invoices are not.

Docs: https://docs.letsrevel.io/compliance/eu/hr/ (docs/compliance/eu/hr.md).
"""

from django.utils.translation import gettext_lazy as _

from events.compliance.base import DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("HR")
class CroatiaPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Croatia: see the module docstring."""

    fiscal_system = _("the Tax Administration's fiscalization system")
