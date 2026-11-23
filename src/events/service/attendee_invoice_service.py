"""Attendee invoice service: validation, generation, editing, delivery, credit notes.

Handles the full lifecycle of attendee invoices issued on behalf of organizers.
"""

import typing as t
from decimal import Decimal
from email.utils import formataddr, parseaddr

import structlog
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import OuterRef, QuerySet, Subquery
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from common.constants import EU_MEMBER_STATES
from common.postal_address import PostalAddress
from common.service.invoice_utils import (
    get_next_sequential_number,
    issuance_statement,
    platform_issuer_name,
    render_pdf,
)
from common.utils import apex_email_domain, is_reserved_mailbox, render_plain_text
from events.compliance import BuyerContext, Decision
from events.compliance import enforcement as compliance
from events.compliance.base import ALL_NEXUS
from events.models.attendee_invoice import (
    AttendeeInvoice,
    AttendeeInvoiceCreditNote,
    BuyerBillingSnapshot,
    TotalsMethod,
)
from events.models.organization import Organization
from events.models.skipped_fiscal_document import SkippedFiscalDocument
from events.models.ticket import Payment
from events.service import skipped_fiscal_document_service, small_business_scheme_service
from events.service.attendee_credit_note_service import (
    create_credit_note,
    load_refunded,
    record_credit_note_for_skipped_invoice,
    record_skipped_credit_note,
)
from events.service.attendee_invoice_lines import (
    _build_line_items,
    _exempt_reasons,
)
from events.service.attendee_vat_service import out_of_scope_for
from events.service.einvoice_service import facturx_pdf_for
from events.service.en16931_totals_service import compute_en16931_totals
from events.service.payment_means_service import fetch_payment_means
from events.service.zero_rate_guard_service import (
    raise_if_zero_rate_tiers_missing_reason,
    zero_rate_tiers_missing_reason,
)
from events.utils.billing_address import snapshot_buyer_postal_address
from events.utils.small_business import is_small_business
from events.utils.supply_date import supply_dates_for
from events.utils.vat_category import (
    VatCategory,
    build_vat_exemptions,
)
from notifications.service.org_sender import org_reply_to

if t.TYPE_CHECKING:
    from uuid import UUID

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Phase 2: Org opt-in validation
# ---------------------------------------------------------------------------


def einvoice_missing_fields(org: Organization) -> list[str]:
    """Return the organization fields still missing for compliant e-invoicing.

    Covers the structured address, then the legal name and the seller's tax identity: a
    VIES-validated VAT ID, or an Austrian small-business scheme with a tax registration number (#1244;
    a German Kleinunternehmer needs the VAT ID, #1258).

    Returns:
        Organization field names (e.g. ``"billing_city"``), in a stable order; ``"vat_id"``
        stands for "VAT ID not VIES-validated" (``"small_business_scheme"`` /
        ``"tax_registration_number"`` for a scheme organizer, see
        :func:`small_business_scheme_service.seller_identity_missing_fields`) and
        ``"vat_exemption_reason"`` for a 0% default rate without an exemption reason.
    """
    missing = [
        f"billing_{PostalAddress.FIELD_SUFFIXES[name]}" for name in org.billing_postal_address().missing_fields()
    ]
    if not org.billing_name:
        missing.append("billing_name")
    missing += small_business_scheme_service.seller_identity_missing_fields(org)
    if zero_rate_tiers_missing_reason(org):
        missing.append("vat_exemption_reason")
    return missing


def validate_invoicing_prerequisites(
    org: Organization, *, require_structured_address: bool = True, require_zero_rate_reasons: bool = True
) -> None:
    """Validate that an organization meets all requirements for attendee invoicing.

    The seller is identified either by a VIES-validated EU VAT ID (an EU VAT country is then required)
    or, for an organizer under the Austrian small-business scheme without a VAT ID (#1244), by its
    declared scheme matching its country of establishment plus a format-valid tax registration number.
    A German Kleinunternehmer needs the VIES-validated USt-IdNr. as well (#1258).

    Args:
        org: The organization.
        require_structured_address: Whether the structured billing address is required. Only turning
            invoicing on needs it; an org that already invoices may keep a legacy text address.
        require_zero_rate_reasons: Whether a paid online 0% tier without an exemption reason is refused.
            Like the address, only turning invoicing on needs it; later edits are guarded where tiers
            and the org default change.

    Raises:
        HttpError 422: If any prerequisite is not met.
    """
    missing: list[str] = []

    # A scheme organizer without a VAT ID is placed by its billing address, not a VAT country.
    if not (is_small_business(org) and not org.vat_id_validated) and (
        not org.vat_country_code or org.vat_country_code not in EU_MEMBER_STATES
    ):
        missing.append(str(_("Organization must be EU-based (valid EU VAT country code required)")))

    missing += small_business_scheme_service.seller_identity_errors(org)

    if not org.billing_name:
        missing.append(str(_("Billing name (legal entity name) is required")))

    if require_structured_address and not org.billing_postal_address().is_complete:
        missing.append(str(_("Billing address (street, postal code, city and country) is required")))

    if missing:
        raise HttpError(422, "; ".join(missing))

    if require_zero_rate_reasons:
        raise_if_zero_rate_tiers_missing_reason(org)


def set_invoicing_mode(org: Organization, mode: Organization.InvoicingMode) -> Organization:
    """Set the invoicing mode for an organization.

    Setting to NONE is always allowed. HYBRID or AUTO requires validation and a
    country whose compliance policy allows Revel-issued attendee invoices.

    Raises:
        CountryComplianceError: If the organization's country blocks attendee invoicing.
        HttpError 422: If any other prerequisite is not met.
    """
    if mode != Organization.InvoicingMode.NONE:
        compliance.assert_attendee_invoicing_allowed(org)
        turning_on = org.invoicing_mode == Organization.InvoicingMode.NONE
        validate_invoicing_prerequisites(
            org, require_structured_address=turning_on, require_zero_rate_reasons=turning_on
        )

    org.invoicing_mode = mode
    org.save(update_fields=["invoicing_mode", "updated_at"])
    return org


# ---------------------------------------------------------------------------
# Phase 4: Invoice generation
# ---------------------------------------------------------------------------


def _sanitize_org_slug(org: Organization, max_len: int) -> str:
    """Sanitize and truncate org slug for use in document number prefixes.

    Removes hyphens, uppercases, and truncates so the full document number
    (prefix + year + sequence) fits within the CharField max_length.
    """
    return org.slug.upper().replace("-", "")[:max_len]


# Invoice number: {SLUG}-{YEAR}-{SEQ:06d} → suffix is 12 chars → max slug = 38
_INVOICE_SLUG_MAX = 38
# Credit note: {SLUG}-CN-{YEAR}-{SEQ:06d} → suffix is 15 chars → max slug = 35
_CREDIT_NOTE_SLUG_MAX = 35


def _get_org_invoice_prefix(org: Organization) -> str:
    """Get the invoice number prefix for an organization."""
    return f"{_sanitize_org_slug(org, _INVOICE_SLUG_MAX)}-"


def generate_attendee_invoice(stripe_session_id: str) -> AttendeeInvoice | None:
    """Generate an attendee invoice for a completed checkout session.

    Args:
        stripe_session_id: The Stripe checkout session ID.

    Returns:
        The created (or already existing) AttendeeInvoice, or None if conditions aren't met.
    """
    # Idempotency first: a retry for a session that already has its invoice returns it, even if
    # the compliance gate below would now refuse (e.g. the event's VAT country changed since).
    # The in-transaction check further down still guards the concurrent-creation race.
    if existing := AttendeeInvoice.objects.filter(stripe_session_id=stripe_session_id).first():
        return existing
    # Likewise, a session whose invoice was already skipped stays skipped (#1091).
    if SkippedFiscalDocument.objects.filter(
        stripe_session_id=stripe_session_id, kind=SkippedFiscalDocument.Kind.INVOICE
    ).exists():
        return None

    payments = list(
        Payment.objects.filter(
            stripe_session_id=stripe_session_id,
            status=Payment.PaymentStatus.SUCCEEDED,
        )
        .select_related(
            "ticket__event__organization__city",
            "ticket__event__venue__city",
            "ticket__event__city",
            "ticket__tier",
            "ticket__discount_code",
        )
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

    event = first_payment.ticket.event
    # Reverse charge: use the authoritative decision from checkout, not re-inferred. An organizer
    # under a small-business scheme never reverse-charges and every line is exempt (E), #1244.
    small_business = is_small_business(org)
    reverse_charge = bool(billing_snapshot.get("reverse_charge", False)) and not small_business
    # The buyer (VAT-ID prefix + checkout VIES outcome) decides the BE/PL domestic-B2B case.
    buyer = BuyerContext.from_billing_snapshot(billing_snapshot)
    out_of_scope = not small_business and out_of_scope_for(
        event, org.vat_country_code, billing_snapshot.get("vat_country_code", "")
    )
    decision = compliance.attendee_invoicing_for_sale(compliance.sale_nexus(org, [event]), buyer)
    if decision.allowed:  # #1244: the seller must be identifiable, and a scheme covers domestic supplies only
        decision = small_business_scheme_service.seller_identity_decision(org)
    if decision.allowed:
        buyer_country = small_business_scheme_service.buyer_country_from_snapshot(billing_snapshot)
        decision = small_business_scheme_service.sale_decision(org, event, buyer_country)
    if not decision.allowed:
        # Defense in depth for orgs that enabled invoicing before the gate, moved
        # country, or sell into a restricted country (EU layer 1, #1057-#1067).
        # Recorded so the organizer knows to issue it from its own system (#1091).
        skipped_fiscal_document_service.record_skipped_invoice(
            payments, _build_line_items(payments, reverse_charge, out_of_scope, small_business), decision
        )
        logger.warning("attendee_invoice_skipped_country_compliance", org_slug=org.slug, session_id=stripe_session_id)
        return None

    # Determine initial status based on invoicing mode
    initial_status = (
        AttendeeInvoice.InvoiceStatus.ISSUED
        if org.invoicing_mode == Organization.InvoicingMode.AUTO
        else AttendeeInvoice.InvoiceStatus.DRAFT
    )

    line_items = _build_line_items(payments, reverse_charge, out_of_scope, small_business)
    vat_exemptions = build_vat_exemptions(
        (VatCategory(item["vat_category"]) for item in line_items), _exempt_reasons(payments, line_items, org)
    )

    supply_date, supply_period_end = supply_dates_for(event)

    # EN 16931 totals (#1234): VAT per (category, rate) group from the charged gross; the
    # per-ticket checkout figures on the lines are rebalanced, the gross is never changed.
    totals = compute_en16931_totals(line_items, reverse_charge)

    # Dominant VAT rate (from first payment). A multi-tier cart (#846) can mix
    # rates; the rendered totals label then drops the rate entirely
    # (``AttendeeInvoice.has_mixed_vat_rates``) rather than claim this one.
    vat_rate = first_payment.vat_rate or Decimal("0.00")

    # Discount info
    discount_codes = {p.ticket.discount_code.code for p in payments if p.ticket.discount_code}
    discount_amount_total = sum(p.ticket.discount_amount or Decimal("0.00") for p in payments)

    # Stripe lookup stays outside the transaction so no API call runs while the numbering lock is held.
    payment_means = fetch_payment_means(org, first_payment.stripe_payment_intent_id or "")

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
            total_gross=totals.total_gross,
            total_net=totals.total_net,
            total_vat=totals.total_vat,
            rounding_amount=totals.rounding_amount,
            totals_method=TotalsMethod.EN16931,
            vat_rate=vat_rate,
            currency=first_payment.currency,
            reverse_charge=reverse_charge,
            discount_code_text=", ".join(sorted(discount_codes)),
            discount_amount_total=discount_amount_total,
            line_items=totals.lines,
            vat_exemptions=vat_exemptions,
            supply_date=supply_date,
            supply_period_end=supply_period_end,
            payment_means_code=payment_means.code,
            payment_method_label=payment_means.label,
            card_last4=payment_means.card_last4,
            # Seller snapshot; the identifiers follow one rule shared with draft issue (#1244, #1258).
            issuer_name=platform_issuer_name(),  # third-party issuer snapshot (#1258)
            seller_name=org.billing_name or org.name,
            seller_small_business_scheme=org.small_business_scheme,  # the PDF then shows no VAT at all (#1258)
            **small_business_scheme_service.seller_identity_snapshot(org, event),
            seller_address=org.billing_address,
            **org.billing_postal_address().to_fields("seller_"),
            seller_email=org.billing_email or org.contact_email or "",
            # Buyer snapshot
            buyer_name=billing_snapshot.get("billing_name", ""),
            buyer_vat_id=billing_snapshot.get("vat_id", ""),
            buyer_vat_country=billing_snapshot.get("vat_country_code", ""),
            buyer_address=billing_snapshot.get("billing_address", ""),
            **snapshot_buyer_postal_address(billing_snapshot).to_fields("buyer_"),
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


def invoice_compliance_decision(invoice: AttendeeInvoice) -> Decision:
    """Whether the country policies still allow issuing (or crediting) this existing invoice.

    For invoices that predate the gate: HYBRID drafts awaiting issue and issued invoices
    about to receive a credit note.
    """
    org, event = invoice.organization, invoice.event
    if org is None:  # organization deleted: only the seller country snapshot is left
        reach = {invoice.seller_vat_country.upper(): ALL_NEXUS}
    else:
        # The event's venue, or the seller-country snapshot when the event is gone.
        reach = compliance.sale_nexus(org, [event] if event else [], [] if event else [invoice.seller_vat_country])
    return compliance.attendee_invoicing_for_sale(reach, _invoice_buyer(invoice))


_NOT_ANNOTATED = object()


def _invoice_buyer(invoice: AttendeeInvoice) -> BuyerContext:
    """The buyer of an existing invoice, with the checkout VIES outcome when it still applies.

    A draft's buyer VAT ID can be edited after checkout; the recorded VIES status only
    belongs to the ID it was taken for, so an edited ID is read from its prefix alone.
    """
    annotated = getattr(invoice, "checkout_billing_snapshot", _NOT_ANNOTATED)  # set by with_issue_context
    if annotated is not _NOT_ANNOTATED:
        snapshot: t.Mapping[str, t.Any] = t.cast("t.Mapping[str, t.Any] | None", annotated) or {}
    else:
        payment = (
            Payment.objects.filter(stripe_session_id=invoice.stripe_session_id).only("buyer_billing_snapshot").first()
        )
        snapshot = (payment.buyer_billing_snapshot if payment else None) or {}
    if snapshot.get("vat_id") != invoice.buyer_vat_id:
        snapshot = {"vat_id": invoice.buyer_vat_id}
    return BuyerContext.from_billing_snapshot(snapshot)


def with_issue_context(qs: QuerySet[AttendeeInvoice]) -> QuerySet[AttendeeInvoice]:
    """Select what :func:`issue_blocked_reason` reads, so listing invoices costs no per-row query."""
    snapshot = Payment.objects.filter(stripe_session_id=OuterRef("stripe_session_id")).values("buyer_billing_snapshot")
    return qs.select_related("organization__city", "event__venue__city", "event__city", "event__organization").annotate(
        checkout_billing_snapshot=Subquery(snapshot[:1])
    )


def issue_blocked_reason(invoice: AttendeeInvoice) -> str:
    """Why a draft can no longer be issued under the country policies (#1091); empty otherwise."""
    if invoice.status != AttendeeInvoice.InvoiceStatus.DRAFT:
        return ""
    return invoice_compliance_decision(invoice).reason


# ---------------------------------------------------------------------------
# Phase 4/5: PDF generation
# ---------------------------------------------------------------------------


def _is_export(invoice: AttendeeInvoice) -> bool:
    """Whether the invoice prints the "Export of services" notice.

    Only for historical zero-rated rows that predate VAT categories (they must re-render as
    issued; admission is no longer zero-rated, #868). Classified rows print their stored
    exemption text instead (``out_of_scope_text`` for O).
    """
    buyer_country = invoice.buyer_vat_country.upper() if invoice.buyer_vat_country else ""
    return bool(
        buyer_country
        and buyer_country not in EU_MEMBER_STATES
        and not invoice.reverse_charge
        and invoice.total_vat == 0
        and not any("vat_category" in item for item in invoice.line_items)
    )


def _is_virtual_interim(invoice: AttendeeInvoice) -> bool:
    """Whether the invoice carries the virtual-event EU B2C interim treatment (#869).

    A cross-border EU B2C buyer of a virtual event legally owes their own
    country's VAT via OSS; we charge the seller's rate as the interim treatment
    and disclose that on the document.
    """
    event = invoice.event
    buyer_country = invoice.buyer_vat_country.upper() if invoice.buyer_vat_country else ""
    return bool(
        event is not None
        and event.is_virtual
        and not invoice.reverse_charge
        and invoice.total_vat > 0
        and buyer_country
        and buyer_country in EU_MEMBER_STATES
        and buyer_country != invoice.seller_vat_country.upper()
    )


def _generate_and_save_pdf(invoice: AttendeeInvoice) -> None:
    """Render the invoice PDF and save it to the invoice's pdf_file field.

    The third-party issuance line (#1258) reads the issuer snapshot; a legacy row with a blank snapshot
    names the current platform operator.
    """
    template = "invoices/attendee_invoice.html"
    rendered_status = invoice.status  # the status this render is for; re-checked under lock before attaching
    context = {
        "invoice": invoice,
        "issuance_statement": issuance_statement(invoice.issuer_name, invoice.seller_name),
        "is_draft": invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT,
        "is_export": _is_export(invoice),
        "is_virtual_interim": _is_virtual_interim(invoice),
    }
    pdf_bytes = facturx_pdf_for(invoice, lambda pdf_a3: render_pdf(template, context, pdf_a3=pdf_a3))
    filename = f"{invoice.invoice_number}.pdf"
    with transaction.atomic():
        # The render is slow and unlocked: the invoice may have been issued (or re-rendered) meanwhile, so
        # only attach to a row still in the state this PDF was rendered for; never overwrite an issued PDF.
        locked = AttendeeInvoice.objects.select_for_update().get(pk=invoice.pk)
        if locked.status != rendered_status or locked.pdf_file:
            invoice.status = locked.status
            invoice.pdf_file = locked.pdf_file
            return
        locked.pdf_file.save(filename, ContentFile(pdf_bytes), save=False)
        locked.save(update_fields=["pdf_file", "updated_at"])  # not a full-row write of a possibly stale instance
        invoice.pdf_file = locked.pdf_file


def ensure_pdf_exists(invoice: AttendeeInvoice) -> None:
    """Generate the invoice PDF if it doesn't exist (on-demand).

    Edits to draft invoices invalidate the PDF; this regenerates it
    when the user requests a download.
    """
    if not invoice.pdf_file:
        _generate_and_save_pdf(invoice)


# ---------------------------------------------------------------------------
# Phase 5: Email delivery
# ---------------------------------------------------------------------------


def _send_org_branded_email(
    invoice: AttendeeInvoice,
    subject: str,
    body: str,
    attachment_path: str,
    attachment_filename: str,
    html_body: str | None = None,
) -> bool:
    """Send an email branded with the org's billing identity.

    Resolves recipient, reply-to, and BCC from the invoice/org. Sent synchronously
    (not ``.delay()``) so the calling retryable task owns retries and can record a
    successful delivery only after the email actually went out (issue #616).

    Returns:
        ``True`` if the email was sent, ``False`` if there was no recipient (a
        permanent failure — retrying won't help). An SMTP failure propagates so
        the caller's Celery task retries.
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
        return False

    # Reply-To only ever points at a verified contact address (#1036). The BCC copy
    # goes to the owner-only billing_email, else the verified contact.
    reply_to = org_reply_to(org) if org else []
    bcc = [org.billing_email] if org and org.billing_email else reply_to

    send_email(
        to=to_email,
        subject=subject,
        body=body,
        html_body=html_body,
        from_email=_invoice_from_address(org_billing_name, org_slug),
        reply_to=reply_to or None,
        bcc=bcc or None,
        attachment_storage_path=attachment_path,
        attachment_filename=attachment_filename,
    )

    logger.info("attendee_invoice_email_sent", subject=subject, invoice_number=invoice.invoice_number)
    return True


def deliver_attendee_invoice(invoice: AttendeeInvoice) -> None:
    """Send the invoice PDF via email to the buyer.

    Self-heals a missing PDF (regenerates on demand) so the recovery sweep can
    recover a document that exhausted its delivery retries, whether it lost the
    PDF or only the email (issue #616). Records the delivery on success.
    """
    from django.template.loader import render_to_string

    from common.models import SiteSettings

    ensure_pdf_exists(invoice)
    if not invoice.pdf_file.name:
        logger.warning("attendee_invoice_no_pdf", invoice_number=invoice.invoice_number)
        return

    event_name = invoice.event.name if invoice.event else "Event"
    email_ctx = {
        "invoice_number": invoice.invoice_number,
        "event_name": event_name,
        "frontend_base_url": SiteSettings.get_solo().frontend_base_url,
    }
    sent = _send_org_branded_email(
        invoice=invoice,
        subject=f"Invoice {invoice.invoice_number} — {event_name}",
        body=render_plain_text("events/emails/attendee_invoice_email.txt", email_ctx),
        html_body=render_to_string("events/emails/attendee_invoice_email.html", email_ctx),
        attachment_path=invoice.pdf_file.name,
        attachment_filename=f"{invoice.invoice_number}.pdf",
    )
    if sent:
        invoice.mark_email_sent()
    else:
        invoice.mark_email_undeliverable(AttendeeInvoice.DeliveryFailureReason.NO_RECIPIENT)


# ---------------------------------------------------------------------------
# Phase 6: Credit notes
# ---------------------------------------------------------------------------


def _locked_unless_draft(
    invoice: AttendeeInvoice,
    stripe_session_id: str,
    refunded_payment_ids: list["UUID"],
    refund_ids: list["UUID"] | None,
) -> AttendeeInvoice | None:
    """Delete a draft that shows the refund; return ``invoice``, locked and re-read, if it was issued (#1486).

    The caller read the draft without a lock, and the organizer can issue it at the same moment.
    A draft is deleted only when the request names a refund (or payment) that the draft shows
    (#1497, ``load_refunded``); if not, the draft stays.

    Returns:
        The issued invoice, or ``None`` when there is nothing to credit: the draft was deleted,
        was kept, or was already gone.
    """
    # Imported here, not at module scope: the draft module imports _generate_and_save_pdf from
    # this one, so a top-level import would close the cycle.
    from events.service.attendee_invoice_draft_service import delete_draft_invoice

    with transaction.atomic():
        locked = AttendeeInvoice.objects.select_for_update().filter(pk=invoice.pk).first()
        if locked is None or locked.status != AttendeeInvoice.InvoiceStatus.DRAFT:
            return locked
        if load_refunded(locked, stripe_session_id, refunded_payment_ids, refund_ids) is not None:
            delete_draft_invoice(locked)
        return None


def generate_attendee_credit_note(
    stripe_session_id: str,
    refunded_payment_ids: list["UUID"],
    refund_ids: list["UUID"] | None = None,
) -> AttendeeInvoiceCreditNote | None:
    """Generate a credit note for refunded payments on an invoiced session.

    A draft invoice that shows a refunded payment is deleted instead of credited. When a country policy refuses
    Revel-issued documents (EU layer 1), or the sale's invoice was itself skipped, no
    credit note is issued and the skip is recorded instead (#1091): the organizer
    corrects the sale in its own compliant system. A refund on a payment that the invoice
    (or the skipped invoice) does not show is not credited (#1485, ``load_refunded``).

    Args:
        stripe_session_id: The Stripe session ID of the original purchase.
        refunded_payment_ids: IDs of the refunded Payment records. Used as-is
            (full payment amount) when ``refund_ids`` is falsy (``None`` or
            ``[]``), for backward compatibility with callers that predate
            per-refund amounts (e.g. in-flight Celery messages).
        refund_ids: IDs of SUCCEEDED Refund rows. When given and non-empty,
            the credit note is keyed on these rows instead of whole payments:
            amounts come from each row's ``amount`` (with pro-rata VAT), and
            idempotency is checked against the refund-id set rather than the
            payment-id set — so two partial refunds on the same payment each
            get their own credit note. An empty list is treated the same as
            ``None`` (falls back to the payment-keyed path) rather than
            silently matching zero rows and returning ``None``.

    Returns:
        The credit note (the existing one for a repeated request), or None when there
        is no issued invoice or nothing new to credit.
    """
    invoice = AttendeeInvoice.objects.filter(stripe_session_id=stripe_session_id).first()
    if not invoice:
        record_credit_note_for_skipped_invoice(stripe_session_id, refunded_payment_ids, refund_ids)
        return None

    if invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT:
        invoice = _locked_unless_draft(invoice, stripe_session_id, refunded_payment_ids, refund_ids)
        if invoice is None:
            return None

    loaded = load_refunded(invoice, stripe_session_id, refunded_payment_ids, refund_ids)
    if loaded is None:
        return None
    refund_rows, refunded_payments = loaded

    decision = invoice_compliance_decision(invoice)
    if not decision.allowed:
        logger.warning("attendee_credit_note_skipped_country_compliance", invoice_number=invoice.invoice_number)
        record_skipped_credit_note(invoice, decision, refund_rows, refunded_payments)
        return None

    org = invoice.organization
    cn_prefix = f"{_sanitize_org_slug(org, _CREDIT_NOTE_SLUG_MAX)}-CN-" if org else "CN-"
    credit_note, created = create_credit_note(invoice, refund_rows, refunded_payments, cn_prefix)
    if credit_note is None:
        return None
    # A new note has no PDF yet; an existing one lost it if a previous attempt failed after its commit.
    ensure_credit_note_pdf_exists(credit_note)
    if created:
        logger.info(
            "attendee_credit_note_generated",
            credit_note_number=credit_note.credit_note_number,
            invoice_number=invoice.invoice_number,
        )
    return credit_note


def _generate_credit_note_pdf(credit_note: AttendeeInvoiceCreditNote) -> None:
    """Render the credit note PDF and save it.

    The credit note intentionally renders seller/buyer info from the parent
    invoice (the issued snapshot) rather than maintaining its own copy.
    """
    template = "invoices/attendee_credit_note.html"
    invoice = credit_note.invoice
    context = {
        "credit_note": credit_note,
        "invoice": invoice,
        "issuance_statement": issuance_statement(credit_note.issuer_name, invoice.seller_name),
        "is_export": _is_export(invoice),
        "is_virtual_interim": _is_virtual_interim(invoice),
    }
    pdf_bytes = facturx_pdf_for(credit_note, lambda pdf_a3: render_pdf(template, context, pdf_a3=pdf_a3))
    filename = f"{credit_note.credit_note_number}.pdf"
    with transaction.atomic():
        locked = AttendeeInvoiceCreditNote.objects.select_for_update().get(pk=credit_note.pk)
        if locked.pdf_file:  # a concurrent render already attached one; keep it
            credit_note.pdf_file = locked.pdf_file
            return
        locked.pdf_file.save(filename, ContentFile(pdf_bytes), save=False)
        locked.save(update_fields=["pdf_file", "updated_at"])
        credit_note.pdf_file = locked.pdf_file


def ensure_credit_note_pdf_exists(credit_note: AttendeeInvoiceCreditNote) -> None:
    """Generate the credit note PDF if it doesn't exist (on-demand)."""
    if not credit_note.pdf_file:
        _generate_credit_note_pdf(credit_note)


def deliver_credit_note(credit_note: AttendeeInvoiceCreditNote) -> None:
    """Send the credit note PDF via email.

    Self-heals a missing PDF and records the delivery on success so the recovery
    sweep can recover an undelivered credit note (issue #616).
    """
    from django.template.loader import render_to_string

    from common.models import SiteSettings

    ensure_credit_note_pdf_exists(credit_note)
    invoice = credit_note.invoice
    if not credit_note.pdf_file.name:
        logger.warning("attendee_credit_note_no_pdf", credit_note_number=credit_note.credit_note_number)
        return

    event_name = invoice.event.name if invoice.event else "Event"
    email_ctx = {
        "credit_note_number": credit_note.credit_note_number,
        "invoice_number": invoice.invoice_number,
        "event_name": event_name,
        "frontend_base_url": SiteSettings.get_solo().frontend_base_url,
    }
    sent = _send_org_branded_email(
        invoice=invoice,
        subject=f"Credit Note {credit_note.credit_note_number} — {event_name}",
        body=render_plain_text("events/emails/attendee_credit_note_email.txt", email_ctx),
        html_body=render_to_string("events/emails/attendee_credit_note_email.html", email_ctx),
        attachment_path=credit_note.pdf_file.name,
        attachment_filename=f"{credit_note.credit_note_number}.pdf",
    )
    if sent:
        credit_note.mark_email_sent()
    else:
        credit_note.mark_email_undeliverable(AttendeeInvoiceCreditNote.DeliveryFailureReason.NO_RECIPIENT)


def _invoice_from_address(billing_name: str, org_slug: str) -> str:
    """``"<billing name>" <slug@apex>``, or the system address for role-mailbox slugs (RFC 2142)."""
    if is_reserved_mailbox(org_slug):
        return formataddr((billing_name, parseaddr(settings.DEFAULT_FROM_EMAIL)[1]))
    return formataddr((billing_name, f"{org_slug}@{apex_email_domain()}"))
