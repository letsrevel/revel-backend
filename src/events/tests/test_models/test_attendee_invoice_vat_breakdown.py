"""VAT-rate breakdown derived from an attendee invoice's line items (#897).

``AttendeeInvoice.vat_rate`` is a scalar snapshot of the FIRST payment. Multi-tier
carts (#846) can mix rates, so the header column cannot describe the document.
These tests pin the derived per-rate breakdown that consumers render instead.

Unsaved model instances throughout: the properties are pure functions of the
``line_items`` JSON and touch no database.
"""

import typing as t
from decimal import Decimal

from events.models.attendee_invoice import AttendeeInvoice, InvoiceLineItemDict


def _line(net: str, vat: str, rate: str, gross: str) -> InvoiceLineItemDict:
    """Build one stored line item; amounts are strings at rest, as the service writes them."""
    return InvoiceLineItemDict(
        description="Event — Tier — Guest",
        unit_price_gross=gross,
        discount_amount="0.00",
        net_amount=net,
        vat_amount=vat,
        vat_rate=rate,
    )


def _invoice(items: list[InvoiceLineItemDict]) -> AttendeeInvoice:
    """An unsaved invoice carrying only the fields these properties read."""
    return AttendeeInvoice(line_items=t.cast(t.Any, items))


class TestVatBreakdown:
    """``AttendeeInvoice.vat_breakdown`` groups line items by VAT rate."""

    def test_mixed_cart_yields_one_bucket_per_rate_ascending(self) -> None:
        """A 22%/10% cart reports two buckets, lowest rate first, each summing its own lines."""
        invoice = _invoice(
            [
                _line(net="40.98", vat="9.02", rate="22.00", gross="50.00"),
                _line(net="109.09", vat="10.91", rate="10.00", gross="120.00"),
                _line(net="81.97", vat="18.03", rate="22.00", gross="100.00"),
            ]
        )

        breakdown = invoice.vat_breakdown

        assert [b["vat_rate"] for b in breakdown] == [Decimal("10.00"), Decimal("22.00")]
        assert breakdown[0] == {
            "vat_rate": Decimal("10.00"),
            "net_amount": Decimal("109.09"),
            "vat_amount": Decimal("10.91"),
            "gross_amount": Decimal("120.00"),
        }
        assert breakdown[1] == {
            "vat_rate": Decimal("22.00"),
            "net_amount": Decimal("122.95"),
            "vat_amount": Decimal("27.05"),
            "gross_amount": Decimal("150.00"),
        }

    def test_buckets_reconcile_to_the_header_totals(self) -> None:
        """Bucket sums equal the invoice totals -- the breakdown never contradicts the header."""
        invoice = _invoice(
            [
                _line(net="40.98", vat="9.02", rate="22.00", gross="50.00"),
                _line(net="109.09", vat="10.91", rate="10.00", gross="120.00"),
            ]
        )
        invoice.total_net = Decimal("150.07")
        invoice.total_vat = Decimal("19.93")
        invoice.total_gross = Decimal("170.00")

        breakdown = invoice.vat_breakdown

        assert sum(b["net_amount"] for b in breakdown) == invoice.total_net
        assert sum(b["vat_amount"] for b in breakdown) == invoice.total_vat
        assert sum(b["gross_amount"] for b in breakdown) == invoice.total_gross

    def test_single_rate_cart_yields_one_bucket(self) -> None:
        """The common path is unchanged: one rate, one bucket, not mixed."""
        invoice = _invoice(
            [
                _line(net="81.97", vat="18.03", rate="22.00", gross="100.00"),
                _line(net="40.98", vat="9.02", rate="22.00", gross="50.00"),
            ]
        )

        assert invoice.vat_breakdown == [
            {
                "vat_rate": Decimal("22.00"),
                "net_amount": Decimal("122.95"),
                "vat_amount": Decimal("27.05"),
                "gross_amount": Decimal("150.00"),
            }
        ]
        assert invoice.has_mixed_vat_rates is False

    def test_empty_line_items_yields_empty_breakdown(self) -> None:
        """A line-item-less invoice has no buckets and is not mixed."""
        invoice = _invoice([])

        assert invoice.vat_breakdown == []
        assert invoice.has_mixed_vat_rates is False

    def test_equal_rates_written_with_different_precision_are_one_bucket(self) -> None:
        """'22.0' and '22.00' are ONE rate.

        The pre-#897 implementation compared raw strings, so this cart reported
        itself as mixed and the PDF dropped a rate label it was entitled to show.
        """
        invoice = _invoice(
            [
                _line(net="81.97", vat="18.03", rate="22.00", gross="100.00"),
                _line(net="40.98", vat="9.02", rate="22.0", gross="50.00"),
            ]
        )

        assert len(invoice.vat_breakdown) == 1
        assert invoice.vat_breakdown[0]["vat_rate"] == Decimal("22.00")
        assert invoice.has_mixed_vat_rates is False


class TestHasMixedVatRates:
    """``has_mixed_vat_rates`` is derived from the breakdown, so the two agree by construction."""

    def test_true_when_breakdown_has_more_than_one_bucket(self) -> None:
        """More than one bucket IS the definition of a mixed-rate invoice."""
        invoice = _invoice(
            [
                _line(net="81.97", vat="18.03", rate="22.00", gross="100.00"),
                _line(net="109.09", vat="10.91", rate="10.00", gross="120.00"),
            ]
        )

        assert invoice.has_mixed_vat_rates is True
        assert len(invoice.vat_breakdown) == 2
