"""Totals of an attendee credit note, by its invoice's method (#1234).

A credit note corrects one invoice, so it is figured the way that invoice was:

- a ``legacy`` invoice gets the pre-EN 16931 arithmetic (the sums of the lines as built,
  which are the payment figures on the payment path and the pro-rata VAT on the refund-row
  path), no rounding, and is stamped ``legacy``;
- an ``en16931`` invoice gets **telescoping** per-group totals: for each ``(category, rate)``
  group on the note, the cumulative gross credited so far (prior notes plus this one) is
  turned into a cumulative net and VAT -- the invoice's own stored figures once the whole
  group is credited, EN 16931 figures for that cumulative gross before -- and the note
  carries the difference from what the prior notes already credited. Each group therefore
  closes exactly when its last cent is credited, whatever the refund order, so credited
  one ticket at a time the notes add back up to the invoice's net, VAT and rounding.

Pure: the caller loads the invoice and its prior credit notes (under the invoice row lock).
"""

import typing as t
from decimal import Decimal

from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote, InvoiceLineItemDict, TotalsMethod
from events.service.en16931_totals_service import ZERO, GroupKey, group_lines, group_net_vat, spread_group_lines


class CreditNoteTotals(t.NamedTuple):
    """The lines and header money columns of a credit note still to be created."""

    lines: list[InvoiceLineItemDict]
    amount_gross: Decimal
    amount_net: Decimal
    amount_vat: Decimal
    rounding_amount: Decimal
    totals_method: TotalsMethod


class _GroupSum(t.NamedTuple):
    gross: Decimal
    net: Decimal
    vat: Decimal


_NOTHING = _GroupSum(ZERO, ZERO, ZERO)


def _legacy(lines: list[InvoiceLineItemDict]) -> CreditNoteTotals:
    return CreditNoteTotals(
        lines=lines,
        amount_gross=sum((Decimal(line["unit_price_gross"]) for line in lines), ZERO),
        amount_net=sum((Decimal(line["net_amount"]) for line in lines), ZERO),
        amount_vat=sum((Decimal(line["vat_amount"]) for line in lines), ZERO),
        rounding_amount=ZERO,
        totals_method=TotalsMethod.LEGACY,
    )


def _group_sums(lines: list[InvoiceLineItemDict], reverse_charge: bool) -> dict[GroupKey, _GroupSum]:
    """Stored ``(gross, net, vat)`` per group of ``lines``."""
    return {
        key: _GroupSum(
            sum((Decimal(lines[i]["unit_price_gross"]) for i in indexes), ZERO),
            sum((Decimal(lines[i]["net_amount"]) for i in indexes), ZERO),
            sum((Decimal(lines[i]["vat_amount"]) for i in indexes), ZERO),
        )
        for key, indexes in group_lines(lines, reverse_charge).items()
    }


def _telescoping(
    invoice: AttendeeInvoice, lines: list[InvoiceLineItemDict], prior: t.Sequence[AttendeeInvoiceCreditNote]
) -> CreditNoteTotals:
    invoice_groups = _group_sums(invoice.line_items, invoice.reverse_charge)
    credited: dict[GroupKey, _GroupSum] = {}
    for credit_note in prior:
        for key, group in _group_sums(credit_note.line_items, invoice.reverse_charge).items():
            before = credited.get(key, _NOTHING)
            credited[key] = _GroupSum(before.gross + group.gross, before.net + group.net, before.vat + group.vat)

    out_lines = [line.copy() for line in lines]
    amount_net = amount_vat = rounding_amount = ZERO
    for key, indexes in group_lines(lines, invoice.reverse_charge).items():
        grosses = [Decimal(lines[i]["unit_price_gross"]) for i in indexes]
        gross = sum(grosses, ZERO)
        before = credited.get(key, _NOTHING)
        cumulative_gross = before.gross + gross
        on_invoice = invoice_groups.get(key)
        if on_invoice is not None and cumulative_gross == on_invoice.gross:
            # The group is now fully credited: land exactly on the invoice's stored figures. A group
            # the invoice does not carry, or whose gross never matches (a DRAFT edited before
            # issuing), can only ever get EN 16931 figures of its own.
            cumulative_net, cumulative_vat = on_invoice.net, on_invoice.vat
        else:
            cumulative_net, cumulative_vat, _unused = group_net_vat(cumulative_gross, key[1])
        net, vat = cumulative_net - before.net, cumulative_vat - before.vat
        # ponytail: a note's VAT is a difference of cumulative figures, not round(net x rate), so
        # its own BR-CO-17 can be a cent off (within the CEN +/-1 tolerance) in exchange for the
        # notes footing to the invoice, group by group, in any refund order. Across groups the
        # header rounding can sum several groups' cents (seen up to 0.03 on one note).
        rounding = gross - net - vat
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
    return _telescoping(invoice, lines, prior)
