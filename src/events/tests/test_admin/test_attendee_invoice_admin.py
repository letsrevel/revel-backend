"""Admin rendering for AttendeeInvoice (#897).

The VAT column must not claim a single rate for a mixed-rate cart -- the same
guard the PDF totals row carries.
"""

import typing as t
from decimal import Decimal

from django.contrib.admin.sites import AdminSite

from events.admin.attendee_invoice import AttendeeInvoiceAdmin
from events.models.attendee_invoice import AttendeeInvoice, InvoiceLineItemDict


def _line(rate: str) -> InvoiceLineItemDict:
    """A stored line item carrying the given VAT rate."""
    return InvoiceLineItemDict(
        description="Event — Tier — Guest",
        unit_price_gross="100.00",
        discount_amount="0.00",
        net_amount="81.97",
        vat_amount="18.03",
        vat_rate=rate,
    )


def _admin() -> AttendeeInvoiceAdmin:
    """The registered admin class, instantiated off a bare site."""
    return AttendeeInvoiceAdmin(AttendeeInvoice, AdminSite())


class TestVatDisplay:
    """``AttendeeInvoiceAdmin.vat_display`` branches like the PDF totals row."""

    def test_single_rate_shows_the_rate(self) -> None:
        """The ordinary invoice still names its rate."""
        invoice = AttendeeInvoice(
            total_vat=Decimal("18.03"),
            vat_rate=Decimal("22.00"),
            reverse_charge=False,
            line_items=t.cast(t.Any, [_line("22.00")]),
        )

        assert _admin().vat_display(invoice) == "18.03 (22.00%)"

    def test_mixed_rates_show_mixed_instead_of_the_first_payments_rate(self) -> None:
        """A 22%/10% invoice must not display '22.00%' -- that rate is one line's, not the document's."""
        invoice = AttendeeInvoice(
            total_vat=Decimal("19.93"),
            vat_rate=Decimal("22.00"),
            reverse_charge=False,
            line_items=t.cast(t.Any, [_line("22.00"), _line("10.00")]),
        )

        rendered = _admin().vat_display(invoice)

        assert rendered == "19.93 (mixed)"
        assert "22.00%" not in rendered

    def test_reverse_charge_wins_over_the_mixed_branch(self) -> None:
        """Reverse charge is checked first: no VAT was charged at any rate."""
        invoice = AttendeeInvoice(
            total_vat=Decimal("0.00"),
            vat_rate=Decimal("22.00"),
            reverse_charge=True,
            line_items=t.cast(t.Any, [_line("22.00"), _line("10.00")]),
        )

        assert _admin().vat_display(invoice) == "RC"
