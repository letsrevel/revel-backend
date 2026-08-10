"""Report attendee invoices issued with the pre-#868 (wrong) VAT treatment.

Admission to events is taxed where the event takes place (Art. 53/54(1) VAT
Directive), so the reverse-charge and non-EU zero-rating branches removed in
#868 under-collected VAT the organizer still owes. This command lists every
affected document so organizers can review corrections with their tax adviser.

Two sets are reported:

- ``reverse_charge``: invoices explicitly flagged ``reverse_charge=True``.
- ``export``: the zero-rated non-EU set. There is no explicit flag for it —
  it is *implied* by ``total_vat == 0`` + a non-EU buyer country on a
  non-reverse-charge invoice (mirrors the historical ``_is_export`` render
  heuristic), which is why the treatment is spelled out as its own column.

Invoices for virtual events are excluded: under the #869 rules those
legitimately carry reverse charge (cross-border EU B2B) or zero VAT (non-EU),
so they are not mischarges. Pass ``--until YYYY-MM-DD`` (exclusive; use the
date the #868 fix was deployed) to bound the report to the historical set —
e.g. it keeps a 0%-rate org's legitimate post-#868 non-EU invoices out of the
implied-export column.

The snapshot ``vat_rate`` on these invoices is 0.00 (that was the bug), so the
uncollected VAT is *estimated* at the organization's currently configured VAT
rate over the invoiced amount (which the buyer paid as net).
"""

import csv
import datetime
import typing as t
from decimal import ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand, CommandParser
from django.db.models import Q

from common.constants import EU_MEMBER_STATES
from events.models import AttendeeInvoice


class Command(BaseCommand):
    help = "List attendee invoices issued with reverse-charge or non-EU zero-rating (wrong for admission, #868)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Add the optional historical cutoff."""
        parser.add_argument(
            "--until",
            type=datetime.date.fromisoformat,
            default=None,
            help="Only report invoices created before this date (exclusive). "
            "Pass the #868 deploy date to bound the report to the historical set.",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Write the affected invoices as CSV to stdout."""
        # Virtual events (#869) legitimately produce reverse-charge (EU B2B) and
        # zero-VAT (non-EU) invoices, so they are excluded — only physical-event
        # documents can carry the pre-#868 mischarge. Excluding via the event FK
        # keeps rows whose event was since deleted (SET_NULL): those predate
        # is_virtual and are exactly the historical set this report is for.
        invoices = (
            AttendeeInvoice.objects.filter(Q(reverse_charge=True) | Q(total_vat=0))
            .exclude(event__is_virtual=True)
            .select_related("organization")
            .order_by("created_at")
        )
        if options["until"] is not None:
            invoices = invoices.filter(created_at__date__lt=options["until"])

        writer = csv.writer(self.stdout)
        writer.writerow(
            [
                "invoice_number",
                "status",
                "organization",
                "buyer_country",
                "treatment",
                "invoiced_amount",
                "currency",
                "org_current_vat_rate",
                "estimated_uncollected_vat",
                "issued_at",
            ]
        )

        count = 0
        for invoice in invoices:
            if invoice.reverse_charge:
                treatment = "reverse_charge"
            elif self._is_zero_rated_export(invoice):
                treatment = "export (implied: total_vat=0 + non-EU buyer country)"
            else:
                continue  # total_vat=0 for other reasons (free tickets, 0% org rate)

            org = invoice.organization
            rate = org.vat_rate if org else Decimal("0.00")
            uncollected = (invoice.total_gross * rate / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            writer.writerow(
                [
                    invoice.invoice_number,
                    invoice.status,
                    org.name if org else "",
                    invoice.buyer_vat_country,
                    treatment,
                    invoice.total_gross,
                    invoice.currency,
                    rate,
                    uncollected,
                    invoice.issued_at.isoformat() if invoice.issued_at else "",
                ]
            )
            count += 1

        self.stderr.write(self.style.SUCCESS(f"{count} affected invoice(s) found."))
        if count:
            self.stderr.write(
                "estimated_uncollected_vat uses the org's CURRENT vat_rate over the invoiced (net) amount — "
                "the snapshot rate on these documents is 0.00 by construction. Review with a tax adviser."
            )

    @staticmethod
    def _is_zero_rated_export(invoice: AttendeeInvoice) -> bool:
        """Whether the invoice matches the implied historical export treatment."""
        buyer_country = invoice.buyer_vat_country.upper() if invoice.buyer_vat_country else ""
        return bool(
            invoice.total_vat == 0
            and invoice.total_gross > 0
            and buyer_country
            and buyer_country not in EU_MEMBER_STATES
        )
