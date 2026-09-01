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
        """Bucket sums equal the header totals when the two are in sync.

        The first line's gross (50.01) deliberately does NOT equal its net + vat
        (50.00), so this also fails if the implementation recomputes a bucket's
        gross as net + vat instead of summing the stored ``unit_price_gross``.

        It does not pin "the breakdown never contradicts the header": the totals
        below are the line-item sums by construction, and nothing in the model
        stops the two diverging -- ``update_draft_invoice`` can PATCH them apart
        (#911). Only a fixture that sets them apart would pin that, and there
        deliberately isn't one, because the model does not yet promise it.
        """
        invoice = _invoice(
            [
                _line(net="40.98", vat="9.02", rate="22.00", gross="50.01"),
                _line(net="109.09", vat="10.91", rate="10.00", gross="120.00"),
            ]
        )
        invoice.total_net = Decimal("150.07")
        invoice.total_vat = Decimal("19.93")
        invoice.total_gross = Decimal("170.01")

        breakdown = invoice.vat_breakdown

        assert sum(b["net_amount"] for b in breakdown) == invoice.total_net
        assert sum(b["vat_amount"] for b in breakdown) == invoice.total_vat
        assert sum(b["gross_amount"] for b in breakdown) == invoice.total_gross

    def test_gross_comes_from_the_stored_gross_not_net_plus_vat(self) -> None:
        """An organizer-edited line whose parts don't foot must not have its gross recomputed.

        Every production writer makes ``net + vat == unit_price_gross`` exact, so this
        is the only fixture that can tell the two implementations apart -- and the
        DRAFT edit path is exactly where a line's parts can stop footing.
        """
        invoice = _invoice([_line(net="80.00", vat="18.03", rate="22.00", gross="100.00")])

        assert invoice.vat_breakdown[0]["gross_amount"] == Decimal("100.00")

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

    def test_sub_cent_rates_are_not_rounded_together(self) -> None:
        """22.003 and 22.004 are TWO rates and must not be merged by the 2dp padding.

        The padding added in #908 rounds by default, which would collapse these into
        a single bucket labelled 22.00 -- a rate neither line carries -- and report a
        genuinely mixed invoice as ``has_mixed_vat_rates is False``. That is the exact
        falsification the property exists to prevent, so the padding must never round.

        Only reachable through a hand-edited draft: ``InvoiceLineItemSchema`` does not
        bound the scale, while every production writer stamps the 2dp column value.
        """
        invoice = _invoice(
            [
                _line(net="40.98", vat="9.02", rate="22.003", gross="50.00"),
                _line(net="81.97", vat="18.03", rate="22.004", gross="100.00"),
            ]
        )

        breakdown = invoice.vat_breakdown

        assert [str(b["vat_rate"]) for b in breakdown] == ["22.003", "22.004"]
        assert invoice.has_mixed_vat_rates is True

    def test_absurd_magnitude_rate_does_not_make_the_invoice_unreadable(self) -> None:
        """A rate too large to pad at 2dp must bucket as-is, never raise.

        ``Decimal("1E+30").quantize(Decimal("0.01"))`` raises ``InvalidOperation``.
        This property is on every read path for an invoice (response schema, admin
        changelist, PDF render), and ninja converts a raised error into a 500 from
        *inside* the view -- so ``ATOMIC_REQUESTS`` sees a clean return and commits
        the offending write. Raising would therefore leave the row permanently
        unreadable and unrepairable, because the PATCH that would fix it renders
        the same schema.
        """
        invoice = _invoice([_line(net="81.97", vat="18.03", rate="1E+30", gross="100.00")])

        breakdown = invoice.vat_breakdown

        assert len(breakdown) == 1
        assert breakdown[0]["vat_rate"] == Decimal("1E+30")
        assert breakdown[0]["gross_amount"] == Decimal("100.00")
        assert invoice.has_mixed_vat_rates is False

    def test_bucket_rate_exponent_is_normalized_to_two_decimal_places(self) -> None:
        """The bucket key must normalize to 2dp regardless of which exponent is seen first.

        ``Decimal("22.0") == Decimal("22.00")`` is True, so a bare ``==`` would not
        catch a bucket that kept the first-seen (1dp) exponent (#908). Pin it with
        ``str()`` instead, and see the "22.0"-first line first to catch the bug.
        """
        invoice = _invoice(
            [
                _line(net="40.98", vat="9.02", rate="22.0", gross="50.00"),
                _line(net="81.97", vat="18.03", rate="22.00", gross="100.00"),
            ]
        )

        assert len(invoice.vat_breakdown) == 1
        assert str(invoice.vat_breakdown[0]["vat_rate"]) == "22.00"


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
