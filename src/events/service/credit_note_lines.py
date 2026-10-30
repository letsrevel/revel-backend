"""Credit-note lines copied from the invoice's own lines (#1250).

A credit note reverses what the invoice shows, not what the ``Payment`` rows say: an organizer
may have edited a DRAFT's rates, descriptions or reverse-charge flag before issuing it. Each
invoice line snapshots the ``payment_id`` it charges (#1242), so a refunded payment finds its
line, which is copied with its rate, category and description, prorated for a partial refund.

A payment whose invoice carries no line for it (an invoice generated before the ids existed, or
a line the organizer removed or added by hand) falls back to the payment-based builders in
:mod:`events.service.attendee_invoice_lines`.

Pure builders: the telescoping that keeps cumulative credit notes within the invoice stays in
:mod:`events.service.credit_note_totals`.
"""

from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from events.models.attendee_invoice import InvoiceLineItemDict
from events.models.refund import Refund
from events.models.ticket import Payment
from events.service.attendee_invoice_lines import ZERO, _build_line_items, _build_refund_line_items
from events.utils.vat_category import VatCategory, derive_vat_category

CENT = Decimal("0.01")


def _lines_by_payment(invoice_lines: list[InvoiceLineItemDict]) -> dict[str, InvoiceLineItemDict]:
    """The invoice's lines keyed by the payment they charge; the first line wins, id-less lines are left out."""
    by_payment: dict[str, InvoiceLineItemDict] = {}
    for line in invoice_lines:
        if payment_id := line.get("payment_id"):
            by_payment.setdefault(payment_id, line)
    return by_payment


def _cumulative(amount: str, refunded: Decimal, paid: Decimal) -> Decimal:
    """``amount`` x ``refunded`` / ``paid`` (capped at the whole), half-up to cents."""
    if refunded >= paid:
        return Decimal(amount)
    return (Decimal(amount) * refunded / paid).quantize(CENT, rounding=ROUND_HALF_UP)


def _credit_line(
    line: InvoiceLineItemDict,
    description: str,
    share: tuple[Decimal, Decimal, Decimal] | None,
    reverse_charge: bool,
    out_of_scope: bool,
) -> InvoiceLineItemDict:
    """``line`` credited for ``share`` = (refunded before, refunded after, paid) amounts (``None`` = in full).

    Keeps the line's rate, category and provenance.

    A full credit repeats the line's stored figures. A reverse-charge invoice never credits VAT
    (EN 16931 BR-AE-05/08), whatever its lines say.
    """
    rate = ZERO if reverse_charge else Decimal(line["vat_rate"])
    if share is None:
        gross, net, vat = Decimal(line["unit_price_gross"]), Decimal(line["net_amount"]), Decimal(line["vat_amount"])
    else:
        # Cumulative, so the notes of one payment add up to the line to the cent: each credits the
        # difference of the rounded cumulative shares, and the last one lands on the line's remainder.
        before, after, paid = share
        gross = _cumulative(line["unit_price_gross"], after, paid) - _cumulative(line["unit_price_gross"], before, paid)
        vat = _cumulative(line["vat_amount"], after, paid) - _cumulative(line["vat_amount"], before, paid)
        net = gross - vat
    if reverse_charge:
        net, vat = gross, ZERO
    credit = line.copy()
    credit["description"] = description
    credit["unit_price_gross"] = str(gross)
    credit["discount_amount"] = str(ZERO)
    credit["net_amount"] = str(net)
    credit["vat_amount"] = str(vat)
    credit["vat_rate"] = str(rate)
    credit["vat_category"] = str(
        VatCategory.REVERSE_CHARGE
        if reverse_charge
        else line.get("vat_category") or derive_vat_category(rate, False, out_of_scope)
    )
    return credit


def credit_lines_for_payments(
    payments: list[Payment], invoice_lines: list[InvoiceLineItemDict], reverse_charge: bool, out_of_scope: bool
) -> list[InvoiceLineItemDict]:
    """Lines crediting whole ``payments`` (the payment-keyed path), one per payment, in order."""
    by_payment = _lines_by_payment(invoice_lines)
    return [
        _credit_line(line, line["description"], None, reverse_charge, out_of_scope)
        if (line := by_payment.get(str(payment.id)))
        else _build_line_items([payment], reverse_charge, out_of_scope)[0]
        for payment in payments
    ]


def credit_lines_for_refunds(
    refund_rows: list[Refund],
    invoice_lines: list[InvoiceLineItemDict],
    reverse_charge: bool,
    out_of_scope: bool,
    already_refunded: dict[UUID, Decimal] | None = None,
) -> list[InvoiceLineItemDict]:
    """Lines crediting ``refund_rows`` (the amount-aware path), one per row, prorated cumulatively.

    ``already_refunded`` is, per payment, what earlier credit notes already cover; the rows of one
    payment are credited in order on top of it.
    """
    by_payment = _lines_by_payment(invoice_lines)
    refunded = dict(already_refunded or {})
    items: list[InvoiceLineItemDict] = []
    for row in refund_rows:
        payment = row.payment
        before = refunded.get(payment.id, ZERO)
        refunded[payment.id] = after = before + row.amount
        line = by_payment.get(str(payment.id))
        if line is None:
            items.extend(_build_refund_line_items([row], reverse_charge, out_of_scope))
            continue
        full = row.amount >= payment.amount
        label = "Refund" if full else "Partial refund"
        share = None if before == 0 and full else (before, after, payment.amount)
        items.append(_credit_line(line, f"{label} — {line['description']}", share, reverse_charge, out_of_scope))
    return items
