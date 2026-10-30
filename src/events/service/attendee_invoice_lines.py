"""Line items of attendee invoices and credit notes, built from Payment and Refund rows.

Split from ``attendee_invoice_service`` at the 1000-line cap (#1234). Pure builders: no
numbering, no PDF, no delivery.
"""

import typing as t
import uuid
from decimal import ROUND_HALF_UP, Decimal

from events.models.attendee_invoice import InvoiceLineItemDict
from events.models.organization import Organization
from events.models.refund import Refund
from events.models.ticket import Payment, Ticket
from events.utils.small_business import is_small_business, scheme_exemption_reason
from events.utils.vat_category import VatCategory, derive_vat_category, effective_vat_exemption_reason

ZERO = Decimal("0.00")


class ProvenanceDict(t.TypedDict, total=False):
    """The optional provenance keys of an :class:`InvoiceLineItemDict`."""

    line_id: str
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
    ids = ProvenanceDict(line_id=str(uuid.uuid4()), payment_id=str(payment.id))
    if payment.ticket.tier_id:
        ids["tier_id"] = str(payment.ticket.tier_id)
    return ids


def _build_line_items(
    payments: list[Payment], reverse_charge: bool = False, out_of_scope: bool = False, small_business: bool = False
) -> list[InvoiceLineItemDict]:
    """Build line item dicts from Payment records, each tagged with its EN 16931 VAT category.

    A reverse-charge (AE) line never carries VAT: the rate is 0 and the net is the gross, whatever
    the payment recorded (a DRAFT switched to reverse charge keeps its payments at the tier's rate).
    So does every line of a new invoice of a small-business-scheme organizer (#1244), which is exempt (E):
    VAT a payment may have recorded before the scheme was declared is never shown (DE § 14c UStG). Credit
    notes are built without the override: they follow the invoice they correct.
    """
    no_vat = reverse_charge or small_business
    items: list[InvoiceLineItemDict] = []
    for payment in payments:
        ticket = payment.ticket
        rate = ZERO if no_vat else (payment.vat_rate or ZERO)
        vat = ZERO if no_vat else (payment.vat_amount or ZERO)
        net = payment.amount if no_vat else (payment.net_amount or payment.amount)
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
    """Collect the exemption reason of each exempt (E) line, in line order (tier's, else the org's).

    A small-business-scheme organizer's statutory note comes first (#1244): DE § 34a S. 1 Nr. 5 UStDV
    and AT § 11 Abs. 1 Z 3 lit. e UStG require it on every invoice, whatever else the tiers say.
    """
    reasons = [scheme_exemption_reason(org.small_business_scheme)] if is_small_business(org) else []
    reasons += [
        effective_vat_exemption_reason(payment.ticket.tier, org)
        if payment.ticket.tier
        else org.vat_exemption_reason.strip()
        for payment, item in zip(payments, line_items, strict=True)
        if item.get("vat_category") == VatCategory.EXEMPT
    ]
    return reasons


def _refund_vat_portion(row: Refund) -> Decimal:
    """Pro-rate a refund row's VAT using its payment's VAT ratio, half-up to cents."""
    payment = row.payment
    if not payment.vat_amount or not payment.amount:
        return ZERO
    return (row.amount * payment.vat_amount / payment.amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _build_refund_line_items(
    refund_rows: list[Refund], reverse_charge: bool = False, out_of_scope: bool = False
) -> list[InvoiceLineItemDict]:
    """Build line item dicts from Refund rows (amount-aware, pro-rata VAT; none under reverse charge).

    A credit note follows the invoice it corrects, so there is no small-business override here (#1244).
    """
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
