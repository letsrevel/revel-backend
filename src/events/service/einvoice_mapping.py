"""Eligibility and EN 16931 mapping of attendee invoices and credit notes (#1235).

Turns the #1234 snapshot of an :class:`AttendeeInvoice` or :class:`AttendeeInvoiceCreditNote` into
a :class:`euinvoice.model.InvoiceDraft` plus the keyword arguments of :func:`euinvoice.calc.complete`.
Each line is mapped with quantity 1 and ``item_net_price`` = the stored net, so BT-131 is our net
exactly and euinvoice derives the VAT breakdown from the same figures we printed. Pure: no I/O.
"""

import datetime
import typing as t
from decimal import Decimal

from django.utils import timezone
from euinvoice import profiles
from euinvoice.calc import ExemptionReason
from euinvoice.model import (
    Buyer,
    BuyerPostalAddress,
    DeliveryInformation,
    Identifier,
    InvoiceDraft,
    InvoicingPeriod,
    ItemInformation,
    LineDraft,
    LineVatInformation,
    PaymentCardInformation,
    PaymentInstructions,
    PrecedingInvoiceReference,
    PriceDetails,
    ProcessControl,
    Seller,
    SellerContact,
    SellerPostalAddress,
)

from common.postal_address import PostalAddress
from events.models.attendee_invoice import (
    AttendeeInvoice,
    AttendeeInvoiceCreditNote,
    InvoiceLineItemDict,
    TotalsMethod,
)
from events.service.payment_means_service import CARD_CODE
from events.utils.vat_category import VatCategory, build_vat_exemptions

INVOICE_TYPE_CODE: t.Final = "380"
CREDIT_NOTE_TYPE_CODE: t.Final = "381"
UNIT_CODE: t.Final = "C62"  # "one" (UN/ECE Rec 20): an admission

Document = AttendeeInvoice | AttendeeInvoiceCreditNote


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def _snapshot_reason(invoice: AttendeeInvoice) -> str | None:
    """Why the invoice's seller/buyer snapshot cannot carry EN 16931 XML, status aside."""
    if invoice.totals_method != TotalsMethod.EN16931:
        return "legacy_totals"
    if not invoice.seller_name.strip():
        return "missing_seller_name"  # BT-27
    if not invoice.buyer_name.strip():
        return "missing_buyer_name"  # BT-44
    if not invoice.seller_vat_id and not invoice.seller_tax_registration_number:
        return "missing_seller_vat_id"  # BT-31, or BT-32 for a small-business seller (#1244)
    if not invoice.seller_country_code:
        return "missing_seller_country"
    if not invoice.buyer_country_code:
        return "missing_buyer_country"
    return None


def _lines_reason(lines: list[InvoiceLineItemDict], buyer_vat_id: str, seller_vat_id: str) -> str | None:
    if any(not item.get("vat_category") for item in lines):
        return "line_without_category"
    if any(item.get("vat_category") == VatCategory.REVERSE_CHARGE for item in lines):
        # BR-AE-02: the seller's VAT identifier (BT-31) and the buyer's (BT-48); BT-32 is not enough here.
        if not seller_vat_id:
            return "reverse_charge_without_seller_vat_id"
        if not buyer_vat_id:
            return "reverse_charge_without_buyer_vat_id"
    if not seller_vat_id and any(item.get("vat_category") == VatCategory.STANDARD for item in lines):
        # BR-S-02: a standard-rated line needs BT-31 (or BT-63); the tax registration identifier (BT-32)
        # alone only carries exempt lines (BR-E-02). A VAT-charging seller that lost its VAT ID gets no XML.
        return "missing_seller_vat_id"
    categories = {item["vat_category"] for item in lines}
    if VatCategory.OUT_OF_SCOPE in categories and len(categories) > 1:
        return "out_of_scope_mixed"  # BR-O-11..14: category O stands alone in the invoice
    return None


def ineligibility_reason(invoice: AttendeeInvoice) -> str | None:
    """Return why ``invoice`` gets no EN 16931 XML, or ``None`` when it is eligible.

    Drafts (watermarked) and legacy rows never carry XML; the rest needs the seller VAT ID
    (BT-31) or, for a small-business seller (#1244) whose lines are all exempt, its tax registration
    number (BT-32: BR-E-02 accepts it, BR-S-02 does not), both countries (BT-40, BT-55), a category on
    every line, both VAT IDs when any line is reverse-charged (BR-AE-02), and no out-of-scope line next
    to another category. A
    CANCELLED invoice (a full credit note cancelled it) stays eligible so a re-render matches the original.
    """
    if invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT:
        return "draft"
    if not invoice.issued_at:
        return "missing_issued_at"
    return _snapshot_reason(invoice) or _lines_reason(invoice.line_items, invoice.buyer_vat_id, invoice.seller_vat_id)


def credit_note_ineligibility_reason(credit_note: AttendeeInvoiceCreditNote) -> str | None:
    """Return why ``credit_note`` gets no EN 16931 XML, or ``None`` when it is eligible.

    The note reads its invoice's snapshot, which may already be CANCELLED (a full credit note
    cancels the invoice before the note's PDF is rendered), and must itself be EN 16931.
    """
    invoice = credit_note.invoice
    if invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT:
        return "draft"
    if not invoice.issued_at or not credit_note.issued_at:
        return "missing_issued_at"
    if credit_note.totals_method != TotalsMethod.EN16931:
        return "legacy_totals"
    return _snapshot_reason(invoice) or _lines_reason(
        credit_note.line_items, invoice.buyer_vat_id, invoice.seller_vat_id
    )


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _opt(value: str) -> str | None:
    return value or None


def _issue_date(issued_at: datetime.datetime) -> datetime.date:
    """BT-2 / BT-26: the calendar date the PDF templates print (``issued_at|date``, the active timezone)."""
    return timezone.localdate(issued_at)


def line_rate(item: InvoiceLineItemDict) -> Decimal | None:
    """BT-152 of a line: its rate, or none for a line not subject to VAT (BR-O-05)."""
    return None if item.get("vat_category") == VatCategory.OUT_OF_SCOPE else Decimal(item["vat_rate"])


def _not_subject_to_vat(lines: list[InvoiceLineItemDict]) -> bool:
    return any(item.get("vat_category") == VatCategory.OUT_OF_SCOPE for item in lines)


def _seller(invoice: AttendeeInvoice, address: PostalAddress, *, vat_id: bool) -> Seller:
    # BR-CO-26 wants BT-29, BT-30 or BT-31 on every seller. Where BR-O-02 forbids the VAT identifier
    # (BT-31), or the seller has none (a small-business organizer identified by its tax registration
    # number alone, BT-32, #1244), the one registered number we hold is also carried as BT-29.
    vat_identifier = _opt(invoice.seller_vat_id) if vat_id else None
    registered = invoice.seller_vat_id or invoice.seller_tax_registration_number
    return Seller(
        name=invoice.seller_name,
        identifiers=(Identifier(value=registered),) if registered and not vat_identifier else (),
        vat_identifier=vat_identifier,
        tax_registration_identifier=_opt(invoice.seller_tax_registration_number),
        postal_address=SellerPostalAddress(
            address_line_1=_opt(address.line1),
            address_line_2=_opt(address.line2),
            city=_opt(address.city),
            post_code=_opt(address.postal_code),
            country_subdivision=_opt(address.subdivision),
            country_code=address.country_code,
        ),
        contact=SellerContact(email=invoice.seller_email) if invoice.seller_email else None,
    )


def _buyer(invoice: AttendeeInvoice, address: PostalAddress, *, vat_id: bool) -> Buyer:
    return Buyer(
        name=invoice.buyer_name,
        vat_identifier=_opt(invoice.buyer_vat_id) if vat_id else None,
        postal_address=BuyerPostalAddress(
            address_line_1=_opt(address.line1),
            address_line_2=_opt(address.line2),
            city=_opt(address.city),
            post_code=_opt(address.postal_code),
            country_subdivision=_opt(address.subdivision),
            country_code=address.country_code,
        ),
    )


def _delivery(invoice: AttendeeInvoice) -> DeliveryInformation | None:
    """BT-72 = the supply date; BG-14 (BT-73/BT-74) only for a multi-day event."""
    if invoice.supply_date is None:
        return None
    period = None
    if invoice.supply_period_end is not None:
        period = InvoicingPeriod(start_date=invoice.supply_date, end_date=invoice.supply_period_end)
    return DeliveryInformation(actual_delivery_date=invoice.supply_date, invoicing_period=period)


def _payment(invoice: AttendeeInvoice) -> PaymentInstructions | None:
    """BG-16 from the payment-means snapshot; BG-18 BT-87 = the card's last four digits for code 48."""
    if not invoice.payment_means_code:
        return None
    card = None
    if invoice.payment_means_code == CARD_CODE and invoice.card_last4:
        card = PaymentCardInformation(primary_account_number=invoice.card_last4)
    return PaymentInstructions(
        payment_means_type_code=invoice.payment_means_code,
        payment_means_text=_opt(invoice.payment_method_label),
        payment_card=card,
    )


def _lines(items: list[InvoiceLineItemDict]) -> tuple[LineDraft, ...]:
    return tuple(
        LineDraft(
            identifier=str(index),
            invoiced_quantity=Decimal(1),
            invoiced_quantity_unit_code=UNIT_CODE,
            price_details=PriceDetails(item_net_price=Decimal(item["net_amount"])),
            vat_information=LineVatInformation(category_code=item["vat_category"], rate=line_rate(item)),
            item=ItemInformation(name=item["description"]),
        )
        for index, item in enumerate(items, start=1)
    )


def _draft(
    invoice: AttendeeInvoice,
    *,
    number: str,
    issued_at: datetime.datetime,
    type_code: str,
    lines: list[InvoiceLineItemDict],
    preceding: tuple[PrecedingInvoiceReference, ...] = (),
) -> InvoiceDraft:
    # BR-O-02: a document not subject to VAT carries neither the seller's nor the buyer's VAT identifier.
    vat_ids = not _not_subject_to_vat(lines)
    return InvoiceDraft(
        number=number,
        issue_date=_issue_date(issued_at),
        type_code=type_code,
        currency_code=invoice.currency,
        process_control=ProcessControl(specification_identifier=profiles.FACTURX_EN16931.specification_identifier),
        preceding_invoice_references=preceding,
        seller=_seller(invoice, invoice.seller_postal_address, vat_id=vat_ids),
        buyer=_buyer(invoice, invoice.buyer_postal_address, vat_id=vat_ids),
        delivery=_delivery(invoice),
        payment_instructions=_payment(invoice),
        lines=_lines(lines),
    )


def invoice_draft_for(invoice: AttendeeInvoice) -> InvoiceDraft:
    """Map an eligible issued invoice to an EN 16931 draft (type 380).

    Raises:
        euinvoice.errors.ModelError: A stored value the model refuses (callers fall back to the plain PDF).
    """
    assert invoice.issued_at is not None  # eligibility, checked by the caller
    return _draft(
        invoice,
        number=invoice.invoice_number,
        issued_at=invoice.issued_at,
        type_code=INVOICE_TYPE_CODE,
        lines=invoice.line_items,
    )


def credit_note_draft_for(credit_note: AttendeeInvoiceCreditNote) -> InvoiceDraft:
    """Map an eligible credit note to an EN 16931 draft (type 381) referencing its invoice (BG-3).

    Raises:
        euinvoice.errors.ModelError: A stored value the model refuses (callers fall back to the plain PDF).
    """
    invoice = credit_note.invoice
    assert invoice.issued_at is not None and credit_note.issued_at is not None  # eligibility
    reference = PrecedingInvoiceReference(reference=invoice.invoice_number, issue_date=_issue_date(invoice.issued_at))
    return _draft(
        invoice,
        number=credit_note.credit_note_number,
        issued_at=credit_note.issued_at,
        type_code=CREDIT_NOTE_TYPE_CODE,
        lines=credit_note.line_items,
        preceding=(reference,),
    )


class CompleteKwargs(t.TypedDict):
    """The keyword arguments :func:`euinvoice.calc.complete` takes besides the draft, as we pass them."""

    paid_amount: Decimal
    rounding_amount: Decimal | None
    exemption_reasons: dict[str, ExemptionReason]


def complete_kwargs_for(document: Document) -> CompleteKwargs:
    """The inputs of :func:`euinvoice.calc.complete` that cannot be derived from the draft.

    BT-113 is the gross the buyer paid (or was refunded), BT-114 the document's own rounding amount,
    and the exemption reasons (BT-120/BT-121) come from the invoice's ``vat_exemptions`` snapshot,
    with the generic texts of :func:`build_vat_exemptions` for any category the snapshot lacks.
    """
    if isinstance(document, AttendeeInvoice):
        invoice, paid = document, document.total_gross
    else:
        invoice, paid = document.invoice, document.amount_gross
    categories = {VatCategory(item["vat_category"]) for item in document.line_items}
    stored = invoice.vat_exemptions or {}
    reasons = {
        category: ExemptionReason(text=_opt(entry["text"]), code=_opt(entry["code"]))
        for category, default in build_vat_exemptions(categories, []).items()
        for entry in (stored.get(category, default),)
    }
    return {
        "paid_amount": paid,
        "rounding_amount": document.rounding_amount or None,
        "exemption_reasons": reasons,
    }
