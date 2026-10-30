"""Per-group telescoping credit-note totals on EN 16931 invoices (#1234, fix wave 2).

Pure: unsaved model instances stand in for the locked invoice and its prior credit notes.
"""

import random
from decimal import ROUND_HALF_UP, Decimal

from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote, InvoiceLineItemDict, TotalsMethod
from events.service.credit_note_totals import credit_note_totals
from events.service.en16931_totals_service import compute_en16931_totals, group_lines

CENT = Decimal("0.01")


def _line(gross: str, rate: str, category: str | None = None) -> InvoiceLineItemDict:
    line = InvoiceLineItemDict(
        description="Ticket",
        unit_price_gross=gross,
        discount_amount="0.00",
        net_amount="0.00",
        vat_amount="0.00",
        vat_rate=rate,
    )
    if category is not None:
        line["vat_category"] = category
    return line


def _invoice(lines: list[InvoiceLineItemDict], stored: list[InvoiceLineItemDict] | None = None) -> AttendeeInvoice:
    """An en16931 invoice over ``lines`` (or ``stored``, when a DRAFT edit changed them)."""
    totals = compute_en16931_totals(stored or lines, reverse_charge=False)
    return AttendeeInvoice(
        totals_method=TotalsMethod.EN16931,
        reverse_charge=False,
        line_items=totals.lines,
        total_gross=totals.total_gross,
        total_net=totals.total_net,
        total_vat=totals.total_vat,
        rounding_amount=totals.rounding_amount,
    )


def _credit(
    invoice: AttendeeInvoice, lines: list[InvoiceLineItemDict], prior: list[AttendeeInvoiceCreditNote]
) -> AttendeeInvoiceCreditNote:
    totals = credit_note_totals(invoice, lines, prior)
    note = AttendeeInvoiceCreditNote(
        amount_gross=totals.amount_gross,
        amount_net=totals.amount_net,
        amount_vat=totals.amount_vat,
        rounding_amount=totals.rounding_amount,
        totals_method=totals.totals_method,
        line_items=totals.lines,
    )
    assert note.amount_net + note.amount_vat + note.rounding_amount == note.amount_gross
    assert sum(Decimal(li["net_amount"]) for li in note.line_items) == note.amount_net
    assert sum(Decimal(li["vat_amount"]) for li in note.line_items) == note.amount_vat
    for (_category, rate), indexes in group_lines(note.line_items, reverse_charge=False).items():
        net = sum(Decimal(note.line_items[i]["net_amount"]) for i in indexes)
        vat = sum(Decimal(note.line_items[i]["vat_amount"]) for i in indexes)
        assert abs(vat - (net * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)) <= CENT
    return note


def _refund_all(invoice: AttendeeInvoice, batches: list[list[InvoiceLineItemDict]]) -> list[AttendeeInvoiceCreditNote]:
    notes: list[AttendeeInvoiceCreditNote] = []
    for batch in batches:
        notes.append(_credit(invoice, batch, notes))
    assert sum(n.amount_gross for n in notes) == invoice.total_gross
    assert sum(n.amount_net for n in notes) == invoice.total_net
    assert sum(n.amount_vat for n in notes) == invoice.total_vat
    assert sum(n.rounding_amount for n in notes) == invoice.rounding_amount
    return notes


class TestTelescoping:
    def test_single_full_refund_mirrors_the_invoice(self) -> None:
        invoice = _invoice([_line("24.90", "13.00"), _line("10.00", "22.00")])
        (note,) = _refund_all(invoice, [[_line("24.90", "13.00"), _line("10.00", "22.00")]])
        assert (note.amount_net, note.amount_vat, note.rounding_amount) == (
            invoice.total_net,
            invoice.total_vat,
            invoice.rounding_amount,
        )
        assert [(li["net_amount"], li["vat_amount"]) for li in note.line_items] == [
            (li["net_amount"], li["vat_amount"]) for li in invoice.line_items
        ]

    def test_two_rate_cart_closes_in_either_order(self) -> None:
        thirteen, twenty_two = _line("24.90", "13.00"), _line("10.00", "22.00")
        invoice = _invoice([thirteen, thirteen, thirteen, twenty_two])
        _refund_all(invoice, [[thirteen], [thirteen], [thirteen], [twenty_two]])
        _refund_all(invoice, [[twenty_two], [thirteen], [thirteen], [thirteen]])

    def test_single_group_examples(self) -> None:
        for gross, rate, count in (("24.90", "13.00", 3), ("24.90", "13.00", 50), ("10.00", "22.00", 10)):
            invoice = _invoice([_line(gross, rate)] * count)
            _refund_all(invoice, [[_line(gross, rate)]] * count)

    def test_property_random_carts_and_refund_splits(self) -> None:
        rng = random.Random(1234)
        grosses = ["10.00", "24.90", "15.50", "7.99", "33.33", "9.95", "0.01"]
        rates = ["13.00", "20.00", "22.00", "10.00", "0.00"]
        for _ in range(300):
            lines = [_line(rng.choice(grosses), rng.choice(rates)) for _ in range(rng.randint(1, 12))]
            invoice = _invoice(lines)
            order = lines[:]
            rng.shuffle(order)
            batches: list[list[InvoiceLineItemDict]] = []
            while order:
                size = rng.randint(1, 3)
                batches.append(order[:size])
                order = order[size:]
            _refund_all(invoice, batches)


class TestEditedDraftGuard:
    def test_group_missing_from_the_invoice_never_takes_its_remainder(self) -> None:
        """Fix B: a DRAFT line edited 20% -> 10% before issuing; the refund lines are rebuilt at 20%."""
        invoice = _invoice([_line("24.90", "20.00")], stored=[_line("24.90", "10.00")])
        totals = credit_note_totals(invoice, [_line("24.90", "20.00")], [])
        assert abs(totals.rounding_amount) <= CENT
        assert (totals.amount_net, totals.amount_vat) == (Decimal("20.75"), Decimal("4.15"))
        assert totals.totals_method == TotalsMethod.EN16931

    def test_group_whose_cumulative_gross_overshoots_the_invoice_does_not_close(self) -> None:
        """Same key on the invoice, but the credited gross never equals the invoice's: EN 16931 on its own."""
        invoice = _invoice([_line("24.90", "13.00")], stored=[_line("20.00", "13.00")])
        totals = credit_note_totals(invoice, [_line("24.90", "13.00")], [])
        assert (totals.amount_net, totals.amount_vat, totals.rounding_amount) == (
            Decimal("22.04"),
            Decimal("2.87"),
            Decimal("-0.01"),
        )
