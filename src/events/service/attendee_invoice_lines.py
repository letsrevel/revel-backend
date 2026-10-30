"""Line items of attendee invoices and credit notes, built from Payment and Refund rows.

Split from ``attendee_invoice_service`` at the 1000-line cap (#1234). Pure builders: no
numbering, no PDF, no delivery.
"""

import typing as t
from decimal import ROUND_HALF_UP, Decimal

from events.models.attendee_invoice import InvoiceLineItemDict
from events.models.organization import Organization
from events.models.refund import Refund
from events.models.ticket import Payment, Ticket
from events.utils.vat_category import VatCategory, derive_vat_category, effective_vat_exemption_reason

ZERO = Decimal("0.00")


class ProvenanceDict(t.TypedDict, total=False):
    """The optional provenance keys of an :class:`InvoiceLineItemDict`."""

    payment_id: str
    tier_id: str


def _description(ticket: Ticket) -> str:
    event_name = ticket.event.name if ticket.event else "Unknown Event"
    tier_name = ticket.tier.name if ticket.tier else "Unknown Tier"
    # A blank holder name is legal since #845 — skip the segment rather than
    # leave a trailing separator on the invoice line.
    return " — ".join(part for part in (event_name, tier_name, ticket.guest_name.strip()) if part)


def _provenance(payment: Payment) -> ProvenanceDict:
    """The ids a line snapshots so a later edit or credit note can find what it charged (#1242, #1250)."""
    ids = ProvenanceDict(payment_id=str(payment.id))
    if payment.ticket.tier_id:
        ids["tier_id"] = str(payment.ticket.tier_id)
    return ids


def _build_line_items(
    payments: list[Payment], reverse_charge: bool = False, out_of_scope: bool = False
) -> list[InvoiceLineItemDict]:
    """Build line item dicts from Payment records, each tagged with its EN 16931 VAT category.

    A reverse-charge (AE) line never carries VAT: the rate is 0 and the net is the gross, whatever
    the payment recorded (a DRAFT switched to reverse charge keeps its payments at the tier's rate).
    """
    items: list[InvoiceLineItemDict] = []
    for payment in payments:
        ticket = payment.ticket
        rate = ZERO if reverse_charge else (payment.vat_rate or ZERO)
        vat = ZERO if reverse_charge else (payment.vat_amount or ZERO)
        net = payment.amount if reverse_charge else (payment.net_amount or payment.amount)
        items.append(
            InvoiceLineItemDict(
                description=_description(ticket),
                unit_price_gross=str(payment.amount),
                discount_amount=str(ticket.discount_amount or ZERO),
                net_amount=str(net),
                vat_amount=str(vat),
                vat_rate=str(rate),
                vat_category=str(derive_vat_category(rate, reverse_charge, out_of_scope)),
                **_provenance(payment),
            )
        )
    return items


def _exempt_reasons(payments: list[Payment], line_items: list[InvoiceLineItemDict], org: Organization) -> list[str]:
    """Collect the exemption reason of each exempt (E) line, in line order (tier's, else the org's)."""
    return [
        effective_vat_exemption_reason(payment.ticket.tier, org)
        if payment.ticket.tier
        else org.vat_exemption_reason.strip()
        for payment, item in zip(payments, line_items, strict=True)
        if item.get("vat_category") == VatCategory.EXEMPT
    ]


def _refund_vat_portion(row: Refund) -> Decimal:
    """Pro-rate a refund row's VAT using its payment's VAT ratio, half-up to cents."""
    payment = row.payment
    if not payment.vat_amount or not payment.amount:
        return ZERO
    return (row.amount * payment.vat_amount / payment.amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _build_refund_line_items(
    refund_rows: list[Refund], reverse_charge: bool = False, out_of_scope: bool = False
) -> list[InvoiceLineItemDict]:
    """Build line item dicts from Refund rows (amount-aware, pro-rata VAT; none under reverse charge)."""
    items: list[InvoiceLineItemDict] = []
    for row in refund_rows:
        payment = row.payment
        label = "Partial refund" if row.amount < payment.amount else "Refund"
        rate = ZERO if reverse_charge else (payment.vat_rate or ZERO)
        vat_amount = ZERO if reverse_charge else _refund_vat_portion(row)
        items.append(
            InvoiceLineItemDict(
                description=f"{label} — {_description(payment.ticket)}",
                unit_price_gross=str(row.amount),
                discount_amount=str(ZERO),
                net_amount=str(row.amount - vat_amount),
                vat_amount=str(vat_amount),
                vat_rate=str(rate),
                vat_category=str(derive_vat_category(rate, reverse_charge, out_of_scope)),
            )
        )
    return items
