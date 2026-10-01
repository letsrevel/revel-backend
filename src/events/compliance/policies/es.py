"""Spain (#1059). Invoicing software must be a VERI*FACTU compliant SIF; Revel is not.

VERI*FACTU applies from 2027-01-01 to corporate taxpayers and from 2027-07-01 to
everyone else. Revel cannot tell the two apart, so the block starts at the earlier
date and attendee invoicing stays allowed until then.

Docs: https://docs.letsrevel.io/compliance/eu/es/ (docs/compliance/eu/es.md).
"""

import datetime

from events.compliance.base import DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("ES")
class SpainPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Spain: see the module docstring."""

    fiscal_system = "Verifactu"
    fiscal_invoicing_from = datetime.date(2027, 1, 1)
