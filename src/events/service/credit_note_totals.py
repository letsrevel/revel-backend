"""Totals of an attendee credit note, by its invoice's method (#1234).

A credit note corrects one invoice, so it is figured the way that invoice was:

- a ``legacy`` invoice gets the pre-EN 16931 arithmetic (the sums of the lines as built,
  which are the payment figures on the payment path and the pro-rata VAT on the refund-row
  path), no rounding, and is stamped ``legacy``;
- an ``en16931`` invoice gets EN 16931 totals on the credit note's own lines, except the
  **closing** credit note -- the one after which every cent of the invoice is credited --
  which instead carries, per ``(category, rate)`` group, whatever net, VAT and rounding
  the earlier credit notes left uncredited. Credited one ticket at a time, the credit
  notes therefore add back up to the invoice's net, VAT and rounding exactly.

Pure: the caller loads the invoice and its prior credit notes (under the invoice row lock).
"""

import typing as t
from decimal import Decimal

from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote, InvoiceLineItemDict, TotalsMethod
from events.service.en16931_totals_service import (
    ZERO,
    GroupKey,
    compute_en16931_totals,
    group_lines,
    spread_group_lines,
)


class CreditNoteTotals(t.NamedTuple):
    """The lines and header money columns of a credit note still to be created."""

    lines: list[InvoiceLineItemDict]
    amount_gross: Decimal
    amount_net: Decimal
    amount_vat: Decimal
    rounding_amount: Decimal
    totals_method: TotalsMethod


def _legacy(lines: list[InvoiceLineItemDict]) -> CreditNoteTotals:
    return CreditNoteTotals(
        lines=lines,
        amount_gross=sum((Decimal(line["unit_price_gross"]) for line in lines), ZERO),
        amount_net=sum((Decimal(line["net_amount"]) for line in lines), ZERO),
        amount_vat=sum((Decimal(line["vat_amount"]) for line in lines), ZERO),
        rounding_amount=ZERO,
        totals_method=TotalsMethod.LEGACY,
    )


def _group_sums(lines: list[InvoiceLineItemDict], reverse_charge: bool) -> dict[GroupKey, tuple[Decimal, Decimal]]:
    """``(net, vat)`` per group of ``lines``."""
    return {
        key: (
            sum((Decimal(lines[i]["net_amount"]) for i in indexes), ZERO),
            sum((Decimal(lines[i]["vat_amount"]) for i in indexes), ZERO),
        )
        for key, indexes in group_lines(lines, reverse_charge).items()
    }


def _closing(
    invoice: AttendeeInvoice, lines: list[InvoiceLineItemDict], prior: t.Sequence[AttendeeInvoiceCreditNote]
) -> CreditNoteTotals:
    """Per group, credit what the invoice carries minus what the prior credit notes already credited."""
    remaining = _group_sums(invoice.line_items, invoice.reverse_charge)
    for credit_note in prior:
        for key, (net, vat) in _group_sums(credit_note.line_items, invoice.reverse_charge).items():
            left_net, left_vat = remaining.get(key, (ZERO, ZERO))
            remaining[key] = (left_net - net, left_vat - vat)

    out_lines = [line.copy() for line in lines]
    amount_net = amount_vat = rounding_amount = ZERO
    for key, indexes in group_lines(lines, invoice.reverse_charge).items():
        grosses = [Decimal(lines[i]["unit_price_gross"]) for i in indexes]
        net, vat = remaining.get(key, (ZERO, ZERO))
        # ponytail: the closing note's VAT is the invoice's remainder, not round(net x rate), so its
        # own BR-CO-17 can be a cent off -- within the CEN +/-1 tolerance -- in exchange for the
        # credit notes footing to the invoice. A group fully credited by earlier partial notes
        # (none of its lines here) cannot be closed and keeps its cent of drift.
        rounding = sum(grosses, ZERO) - net - vat
        for i, (line_net, line_vat) in zip(indexes, spread_group_lines(net, rounding, grosses), strict=True):
            out_lines[i]["net_amount"] = str(line_net)
            out_lines[i]["vat_amount"] = str(line_vat)
        amount_net += net
        amount_vat += vat
        rounding_amount += rounding
    return CreditNoteTotals(
        lines=out_lines,
        amount_gross=sum((Decimal(line["unit_price_gross"]) for line in lines), ZERO),
        amount_net=amount_net,
        amount_vat=amount_vat,
        rounding_amount=rounding_amount,
        totals_method=TotalsMethod.EN16931,
    )


def credit_note_totals(
    invoice: AttendeeInvoice, lines: list[InvoiceLineItemDict], prior: t.Sequence[AttendeeInvoiceCreditNote]
) -> CreditNoteTotals:
    """The totals of a credit note over ``lines`` on ``invoice``, given its ``prior`` credit notes.

    Args:
        invoice: The locked invoice row being corrected.
        lines: The credit note's lines as built (gross, rate and category are what matters).
        prior: Every credit note the invoice already has, read under the same lock.

    Returns:
        The (possibly rebalanced) lines and the header columns; see the module docstring for which
        arithmetic applies. ``amount_gross`` is always the sum of the line grosses.
    """
    if invoice.totals_method != TotalsMethod.EN16931:
        return _legacy(lines)
    totals = compute_en16931_totals(lines, invoice.reverse_charge)
    credited = sum((credit_note.amount_gross for credit_note in prior), ZERO)
    if credited + totals.total_gross == invoice.total_gross:
        return _closing(invoice, lines, prior)
    return CreditNoteTotals(
        lines=totals.lines,
        amount_gross=totals.total_gross,
        amount_net=totals.total_net,
        amount_vat=totals.total_vat,
        rounding_amount=totals.rounding_amount,
        totals_method=TotalsMethod.EN16931,
    )
