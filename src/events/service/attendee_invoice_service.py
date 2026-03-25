"""Attendee invoice service: validation, generation, editing, delivery, credit notes.

Handles the full lifecycle of attendee invoices issued on behalf of organizers.
"""

import typing as t
from decimal import Decimal

import structlog
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from common.constants import EU_MEMBER_STATES
from common.service.invoice_utils import get_next_sequential_number, render_pdf
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote, BuyerBillingSnapshot
from events.models.organization import Organization
from events.models.ticket import Payment

if t.TYPE_CHECKING:
    from uuid import UUID

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Phase 2: Org opt-in validation
# ---------------------------------------------------------------------------


def validate_invoicing_prerequisites(org: Organization) -> None:
    """Validate that an organization meets all requirements for attendee invoicing.

    Raises:
        HttpError 422: If any prerequisite is not met.
    """
    missing: list[str] = []

    if not org.vat_country_code or org.vat_country_code not in EU_MEMBER_STATES:
        missing.append("Organization must be EU-based (valid EU VAT country code required)")

    if not org.vat_id_validated:
        missing.append("VAT ID must be validated via VIES")

    if not org.billing_name:
        missing.append("Billing name (legal entity name) is required")

    if not org.billing_address:
        missing.append("Billing address is required")

    if missing:
        raise HttpError(422, "; ".join(missing))


def set_invoicing_mode(org: Organization, mode: Organization.InvoicingMode) -> Organization:
    """Set the invoicing mode for an organization.

    Setting to NONE is always allowed. HYBRID or AUTO requires validation.
    """
    if mode != Organization.InvoicingMode.NONE:
        validate_invoicing_prerequisites(org)

    org.invoicing_mode = mode
    org.save(update_fields=["invoicing_mode", "updated_at"])
    return org


# ---------------------------------------------------------------------------
# Phase 4: Invoice generation
# ---------------------------------------------------------------------------


def _build_line_items(payments: list[Payment]) -> list[dict[str, str]]:
    """Build line item dicts from Payment records."""
    items = []
    for payment in payments:
        ticket = payment.ticket
        event_name = ticket.event.name if ticket.event else "Unknown Event"
        tier_name = ticket.tier.name if ticket.tier else "Unknown Tier"
        description = f"{event_name} — {tier_name} — {ticket.guest_name}"
        items.append(
            {
                "description": description,
                "unit_price_gross": str(payment.amount),
                "discount_amount": str(ticket.discount_amount or Decimal("0.00")),
                "net_amount": str(payment.net_amount or payment.amount),
                "vat_amount": str(payment.vat_amount or Decimal("0.00")),
                "vat_rate": str(payment.vat_rate or Decimal("0.00")),
            }
        )
    return items


def _get_org_invoice_prefix(org: Organization) -> str:
    """Get the invoice number prefix for an organization."""
    return f"{org.slug.upper().replace('-', '')}-"


def generate_attendee_invoice(stripe_session_id: str) -> AttendeeInvoice | None:
    """Generate an attendee invoice for a completed checkout session.

    Args:
        stripe_session_id: The Stripe checkout session ID.

    Returns:
        The created AttendeeInvoice, or None if conditions aren't met.
    """
    payments = list(
        Payment.objects.filter(
            stripe_session_id=stripe_session_id,
            status=Payment.PaymentStatus.SUCCEEDED,
        )
        .select_related("ticket__event__organization", "ticket__tier")
        .order_by("created_at")
    )

    if not payments:
        return None

    # Check: buyer billing info was provided
    first_payment = payments[0]
    billing_snapshot: BuyerBillingSnapshot | None = first_payment.buyer_billing_snapshot
    if not billing_snapshot:
        return None

    # Check: org has invoicing enabled
    org = first_payment.ticket.event.organization
    if not org or org.invoicing_mode == Organization.InvoicingMode.NONE:
        return None

    # Determine initial status based on invoicing mode
    initial_status = (
        AttendeeInvoice.InvoiceStatus.ISSUED
        if org.invoicing_mode == Organization.InvoicingMode.AUTO
        else AttendeeInvoice.InvoiceStatus.DRAFT
    )

    event = first_payment.ticket.event
    line_items = _build_line_items(payments)

    # Aggregate totals from payments
    total_gross = sum(p.amount for p in payments)
    total_net = sum(p.net_amount or p.amount for p in payments)
    total_vat = sum(p.vat_amount or Decimal("0.00") for p in payments)

    # Dominant VAT rate (from first payment)
    vat_rate = first_payment.vat_rate or Decimal("0.00")

    # Reverse charge: true if any payment has 0 VAT and buyer has a VAT ID
    reverse_charge = bool(
        billing_snapshot.get("vat_id") and billing_snapshot.get("vat_id_validated") and total_vat == Decimal("0.00")
    )

    # Discount info
    discount_codes = {p.ticket.discount_code.code for p in payments if p.ticket.discount_code}
    discount_amount_total = sum(p.ticket.discount_amount or Decimal("0.00") for p in payments)

    with transaction.atomic():
        # Idempotency: check inside transaction to prevent race conditions
        existing = AttendeeInvoice.objects.filter(stripe_session_id=stripe_session_id).first()
        if existing:
            return existing

        prefix = _get_org_invoice_prefix(org)
        year = timezone.now().year
        invoice_number = get_next_sequential_number(AttendeeInvoice, prefix, year, "invoice_number")

        invoice = AttendeeInvoice.objects.create(
            organization=org,
            event=event,
            user=first_payment.user,
            stripe_session_id=stripe_session_id,
            invoice_number=invoice_number,
            status=initial_status,
            total_gross=total_gross,
            total_net=total_net,
            total_vat=total_vat,
            vat_rate=vat_rate,
            currency=first_payment.currency,
            reverse_charge=reverse_charge,
            discount_code_text=", ".join(sorted(discount_codes)),
            discount_amount_total=discount_amount_total,
            line_items=line_items,
            # Seller snapshot
            seller_name=org.billing_name or org.name,
            seller_vat_id=org.vat_id,
            seller_vat_country=org.vat_country_code,
            seller_address=org.billing_address,
            seller_email=org.billing_email or org.contact_email or "",
            # Buyer snapshot
            buyer_name=billing_snapshot.get("billing_name", ""),
            buyer_vat_id=billing_snapshot.get("vat_id", ""),
            buyer_vat_country=billing_snapshot.get("vat_country_code", ""),
            buyer_address=billing_snapshot.get("billing_address", ""),
            buyer_email=billing_snapshot.get("billing_email", ""),
            issued_at=timezone.now() if initial_status == AttendeeInvoice.InvoiceStatus.ISSUED else None,
        )

    # Generate PDF outside transaction (WeasyPrint is slow)
    _generate_and_save_pdf(invoice)

    logger.info(
        "attendee_invoice_generated",
        invoice_number=invoice.invoice_number,
        status=invoice.status,
        org_slug=org.slug,
        session_id=stripe_session_id,
    )

    return invoice


# ---------------------------------------------------------------------------
# Phase 4/5: PDF generation
# ---------------------------------------------------------------------------


def _generate_and_save_pdf(invoice: AttendeeInvoice) -> None:
    """Render the invoice PDF and save it to the invoice's pdf_file field."""
    template = "invoices/attendee_invoice.html"
    context = {
        "invoice": invoice,
        "is_draft": invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT,
    }
    pdf_bytes = render_pdf(template, context)
    filename = f"{invoice.invoice_number}.pdf"
    invoice.pdf_file.save(filename, ContentFile(pdf_bytes), save=True)


def ensure_pdf_exists(invoice: AttendeeInvoice) -> None:
    """Generate the invoice PDF if it doesn't exist (on-demand).

    Edits to draft invoices invalidate the PDF; this regenerates it
    when the user requests a download.
    """
    if not invoice.pdf_file:
        _generate_and_save_pdf(invoice)


# ---------------------------------------------------------------------------
# Phase 5: Editing, issuing, deletion
# ---------------------------------------------------------------------------


EDITABLE_DRAFT_FIELDS = frozenset(
    {
        "buyer_name",
        "buyer_vat_id",
        "buyer_vat_country",
        "buyer_address",
        "buyer_email",
        "total_gross",
        "total_net",
        "total_vat",
        "vat_rate",
        "currency",
        "reverse_charge",
        "discount_code_text",
        "discount_amount_total",
        "line_items",
    }
)


def update_draft_invoice(
    invoice: AttendeeInvoice,
    update_data: dict[str, t.Any],
) -> AttendeeInvoice:
    """Update a DRAFT invoice. Only drafts are editable.

    Args:
        invoice: The invoice to update.
        update_data: Dict of fields to update (from exclude_unset).

    Returns:
        The updated invoice.

    Raises:
        HttpError 409: If the invoice is not a draft.
        HttpError 422: If update_data contains disallowed fields.
    """
    if invoice.status != AttendeeInvoice.InvoiceStatus.DRAFT:
        raise HttpError(409, str(_("Only draft invoices can be edited.")))

    if not update_data:
        return invoice

    disallowed = set(update_data.keys()) - EDITABLE_DRAFT_FIELDS
    if disallowed:
        raise HttpError(422, f"Cannot edit fields: {', '.join(sorted(disallowed))}")

    for field, value in update_data.items():
        setattr(invoice, field, value)

    # Invalidate stale PDF so it gets regenerated on next download
    if invoice.pdf_file:
        invoice.pdf_file.delete(save=False)
        update_data["pdf_file"] = ""

    invoice.save(update_fields=[*update_data.keys(), "updated_at"])

    return invoice


def issue_draft_invoice(invoice: AttendeeInvoice) -> AttendeeInvoice:
    """Issue a DRAFT invoice: finalize, set issued_at, regenerate PDF.

    Args:
        invoice: The draft invoice to issue.

    Returns:
        The issued invoice.

    Raises:
        HttpError 409: If the invoice is not a draft.
    """
    if invoice.status != AttendeeInvoice.InvoiceStatus.DRAFT:
        raise HttpError(409, str(_("Only draft invoices can be issued.")))

    invoice.status = AttendeeInvoice.InvoiceStatus.ISSUED
    invoice.issued_at = timezone.now()
    invoice.save(update_fields=["status", "issued_at", "updated_at"])

    # Regenerate PDF without DRAFT watermark
    _generate_and_save_pdf(invoice)

    return invoice


def delete_draft_invoice(invoice: AttendeeInvoice) -> None:
    """Delete a DRAFT invoice.

    Raises:
        HttpError 409: If the invoice is not a draft.
    """
    if invoice.status != AttendeeInvoice.InvoiceStatus.DRAFT:
        raise HttpError(409, str(_("Only draft invoices can be deleted.")))

    # Delete PDF file if it exists
    if invoice.pdf_file:
        invoice.pdf_file.delete(save=False)

    invoice.delete()


# ---------------------------------------------------------------------------
# Phase 5: Email delivery
# ---------------------------------------------------------------------------


def _send_org_branded_email(
    invoice: AttendeeInvoice,
    subject: str,
    body: str,
    attachment_path: str,
    attachment_filename: str,
) -> None:
    """Send an email branded with the org's billing identity.

    Resolves recipient, reply-to, and BCC from the invoice/org.
    """
    from common.tasks import send_email

    org = invoice.organization
    org_slug = org.slug if org else "unknown"
    org_billing_name = invoice.seller_name

    to_email = invoice.buyer_email
    if not to_email and invoice.user:
        to_email = invoice.user.email
    if not to_email:
        logger.warning("attendee_invoice_no_recipient", invoice_number=invoice.invoice_number)
        return

    reply_to_email = ""
    bcc_email = ""
    if org:
        reply_to_email = org.billing_email or org.contact_email or ""
        bcc_email = org.billing_email or org.contact_email or ""

    send_email.delay(
        to=to_email,
        subject=subject,
        body=body,
        from_email=f"{org_billing_name} <{org_slug}@letsrevel.io>",
        reply_to=[reply_to_email] if reply_to_email else None,
        bcc=[bcc_email] if bcc_email else None,
        attachment_storage_path=attachment_path,
        attachment_filename=attachment_filename,
    )

    logger.info("attendee_invoice_email_sent", subject=subject, to=to_email)


def deliver_attendee_invoice(invoice: AttendeeInvoice) -> None:
    """Send the invoice PDF via email to the buyer."""
    if not invoice.pdf_file:
        logger.warning("attendee_invoice_no_pdf", invoice_number=invoice.invoice_number)
        return

    event_name = invoice.event.name if invoice.event else "Event"
    _send_org_branded_email(
        invoice=invoice,
        subject=f"Invoice {invoice.invoice_number} — {event_name}",
        body=f"Please find attached invoice {invoice.invoice_number} for your purchase at {event_name}.",
        attachment_path=invoice.pdf_file.name,
        attachment_filename=f"{invoice.invoice_number}.pdf",
    )


# ---------------------------------------------------------------------------
# Phase 6: Credit notes
# ---------------------------------------------------------------------------


def generate_attendee_credit_note(
    stripe_session_id: str,
    refunded_payment_ids: list["UUID"],
) -> AttendeeInvoiceCreditNote | None:
    """Generate a credit note for refunded payments on an invoiced session.

    Args:
        stripe_session_id: The Stripe session ID of the original purchase.
        refunded_payment_ids: IDs of the refunded Payment records.

    Returns:
        The created credit note, or None if no invoice exists.
    """
    invoice = AttendeeInvoice.objects.filter(stripe_session_id=stripe_session_id).first()
    if not invoice:
        return None

    # If invoice is still a draft, just delete it instead of creating a credit note
    if invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT:
        delete_draft_invoice(invoice)
        return None

    refunded_payments = list(
        Payment.objects.filter(id__in=refunded_payment_ids).select_related("ticket__event", "ticket__tier")
    )

    if not refunded_payments:
        return None

    # Idempotency: check if a credit note already exists for these specific payments
    existing_cn = (
        AttendeeInvoiceCreditNote.objects.filter(
            invoice=invoice,
            payments__in=refunded_payments,
        )
        .distinct()
        .first()
    )
    if existing_cn:
        return existing_cn

    amount_gross = sum(p.amount for p in refunded_payments)
    amount_net = sum(p.net_amount or p.amount for p in refunded_payments)
    amount_vat = sum(p.vat_amount or Decimal("0.00") for p in refunded_payments)

    line_items = _build_line_items(refunded_payments)
    org = invoice.organization

    with transaction.atomic():
        cn_prefix = f"{org.slug.upper().replace('-', '')}-CN-" if org else "CN-"
        year = timezone.now().year
        credit_note_number = get_next_sequential_number(
            AttendeeInvoiceCreditNote, cn_prefix, year, "credit_note_number"
        )

        credit_note = AttendeeInvoiceCreditNote.objects.create(
            invoice=invoice,
            credit_note_number=credit_note_number,
            amount_gross=amount_gross,
            amount_net=amount_net,
            amount_vat=amount_vat,
            line_items=line_items,
            issued_at=timezone.now(),
        )
        credit_note.payments.set(refunded_payments)

        # Check if fully credited → mark invoice as CANCELLED
        total_credited = sum(cn.amount_gross for cn in invoice.credit_notes.all())
        if total_credited >= invoice.total_gross:
            invoice.status = AttendeeInvoice.InvoiceStatus.CANCELLED
            invoice.save(update_fields=["status", "updated_at"])

    # Generate credit note PDF
    _generate_credit_note_pdf(credit_note)

    # Deliver via email
    _deliver_credit_note(credit_note)

    logger.info(
        "attendee_credit_note_generated",
        credit_note_number=credit_note.credit_note_number,
        invoice_number=invoice.invoice_number,
    )

    return credit_note


def _generate_credit_note_pdf(credit_note: AttendeeInvoiceCreditNote) -> None:
    """Render the credit note PDF and save it."""
    template = "invoices/attendee_credit_note.html"
    context = {"credit_note": credit_note, "invoice": credit_note.invoice}
    pdf_bytes = render_pdf(template, context)
    filename = f"{credit_note.credit_note_number}.pdf"
    credit_note.pdf_file.save(filename, ContentFile(pdf_bytes), save=True)


def _deliver_credit_note(credit_note: AttendeeInvoiceCreditNote) -> None:
    """Send the credit note PDF via email."""
    invoice = credit_note.invoice
    if not credit_note.pdf_file:
        return

    event_name = invoice.event.name if invoice.event else "Event"
    _send_org_branded_email(
        invoice=invoice,
        subject=f"Credit Note {credit_note.credit_note_number} — {event_name}",
        body=(
            f"Please find attached credit note {credit_note.credit_note_number} for invoice {invoice.invoice_number}."
        ),
        attachment_path=credit_note.pdf_file.name,
        attachment_filename=f"{credit_note.credit_note_number}.pdf",
    )
