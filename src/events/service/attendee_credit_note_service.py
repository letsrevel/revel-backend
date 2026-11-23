"""Attendee credit notes: what a refund credits, and the note (or the skipped record) for it.

Split from ``attendee_invoice_service`` when that module reached the 1000-line cap (#1485). The
seam is the credit decision: which refunds a document can credit, the idempotency on refund ids,
the lines, and the row that records them. The entry point
(``attendee_invoice_service.generate_attendee_credit_note``), the PDF rendering and the delivery
stay there, so this module does not import that one.
"""

import typing as t
from decimal import Decimal
from uuid import UUID

import structlog
from django.db import transaction
from django.utils import timezone

from common.service.invoice_utils import get_next_sequential_number, platform_issuer_name
from events.compliance import Decision
from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote, InvoiceLineItemDict
from events.models.refund import Refund
from events.models.skipped_fiscal_document import SkippedFiscalDocument
from events.models.ticket import Payment
from events.service import skipped_fiscal_document_service
from events.service.attendee_vat_service import invoice_out_of_scope, out_of_scope_for
from events.service.credit_note_lines import credit_lines_for_payments, credit_lines_for_refunds
from events.service.credit_note_totals import credit_note_totals

logger = structlog.get_logger(__name__)


class _CreditNoteBuild(t.NamedTuple):
    """The lines and coverage of a credit note still to be created (or recorded as skipped).

    Carries no totals: a Revel credit note derives them from these lines at creation by its
    invoice's method (``credit_note_totals``, #1234), while a skipped record sums them as they are.
    """

    line_items: list[InvoiceLineItemDict]
    payments: t.Collection[Payment]
    refund_rows: list[Refund] | None


def invoiced_payment_ids(source: AttendeeInvoice | SkippedFiscalDocument) -> set[str]:
    """The payments that the sale's document shows.

    For an invoice, the ``payment_id`` of its lines; for a skipped invoice record, its payments.
    A payment that was fully refunded before the document was generated is not in the set. The set
    is empty for an invoice whose lines carry no ``payment_id`` (generated before the ids existed).
    """
    if isinstance(source, AttendeeInvoice):
        return {line["payment_id"] for line in source.line_items if line.get("payment_id")}
    return {str(pk) for pk in source.payments.values_list("pk", flat=True)}


def _build_from_payments(
    refunded_payments: list[Payment],
    existing_cns: list[AttendeeInvoiceCreditNote],
    reverse_charge: bool = False,
    out_of_scope: bool = False,
    invoice_lines: list[InvoiceLineItemDict] | None = None,
) -> AttendeeInvoiceCreditNote | _CreditNoteBuild | None:
    """Legacy payment-keyed path: full payment amounts, exact-set idempotency on payment ids."""
    already_credited_ids: set[UUID] = set()
    for existing_cn in existing_cns:
        already_credited_ids.update(p.id for p in existing_cn.payments.all())

    # Exact-match idempotency: if this exact set was already credited, return it
    refunded_id_set = {p.id for p in refunded_payments}
    for existing_cn in existing_cns:
        if {p.id for p in existing_cn.payments.all()} == refunded_id_set:
            return existing_cn

    # Exclude payments already credited (prevents double-credit from superset retries)
    refunded_payments = [p for p in refunded_payments if p.id not in already_credited_ids]
    if not refunded_payments:
        return None

    return _CreditNoteBuild(
        line_items=credit_lines_for_payments(refunded_payments, invoice_lines or [], reverse_charge, out_of_scope),
        payments=refunded_payments,
        refund_rows=None,
    )


def _build_from_refund_rows(
    refund_rows: list[Refund],
    existing_cns: list[AttendeeInvoiceCreditNote],
    reverse_charge: bool = False,
    out_of_scope: bool = False,
    invoice_lines: list[InvoiceLineItemDict] | None = None,
    skipped_before: t.Sequence[SkippedFiscalDocument] | None = None,
) -> AttendeeInvoiceCreditNote | _CreditNoteBuild | None:
    """Amount-aware, refund-row-keyed path: pro-rata VAT, exact-set idempotency on refund ids.

    Legacy CNs have an empty ``refunds`` M2M; they cover their full ``payments``
    set instead. A refund row whose payment already has a legacy CN is treated
    as fully credited and skipped.
    """
    already_credited_refund_ids: set[UUID] = set()
    legacy_credited_payment_ids: set[UUID] = set()
    already_refunded: dict[UUID, Decimal] = {}  # per payment, for cumulative proration
    for existing_cn in existing_cns:
        cn_refunds = list(existing_cn.refunds.all())
        if cn_refunds:
            already_credited_refund_ids.update(r.id for r in cn_refunds)
        else:
            legacy_credited_payment_ids.update(p.id for p in existing_cn.payments.all())
        for refund in cn_refunds:
            already_refunded[refund.payment_id] = (
                already_refunded.get(refund.payment_id, Decimal("0.00")) + refund.amount
            )
    # Refunds already recorded as skipped notes count towards the proration too.
    for doc in skipped_before or []:
        for refund in doc.refunds.all():
            already_refunded[refund.payment_id] = (
                already_refunded.get(refund.payment_id, Decimal("0.00")) + refund.amount
            )

    # Exact-match idempotency on the refund-id set
    requested_refund_id_set = {r.id for r in refund_rows}
    for existing_cn in existing_cns:
        if {r.id for r in existing_cn.refunds.all()} == requested_refund_id_set:
            return existing_cn

    refund_rows = [
        r
        for r in refund_rows
        if r.id not in already_credited_refund_ids and r.payment_id not in legacy_credited_payment_ids
    ]
    if not refund_rows:
        return None

    return _CreditNoteBuild(
        line_items=credit_lines_for_refunds(
            refund_rows, invoice_lines or [], reverse_charge, out_of_scope, already_refunded
        ),
        payments={r.payment for r in refund_rows},
        refund_rows=refund_rows,
    )


def load_refunded(
    source: AttendeeInvoice | SkippedFiscalDocument,
    stripe_session_id: str,
    refunded_payment_ids: list[UUID],
    refund_ids: list[UUID] | None,
) -> tuple[list[Refund], list[Payment]] | None:
    """The refund rows (amount-aware path) or refunded payments (legacy path) that ``source`` can credit.

    A credit note reverses what the sale's document shows, so a refund (or a legacy payment) whose
    payment is not on ``source`` is left out (#1485): each caller of the credit-note generation gets
    the same rule.
    An attendee invoice that names no payment (see :func:`invoiced_payment_ids`) is not filtered;
    a skipped record with no payment credits nothing.

    Returns:
        ``(refund_rows, [])`` or ``([], refunded_payments)``; ``None`` when nothing matches.
    """
    on_document = invoiced_payment_ids(source)
    # Only an attendee invoice without payment ids (generated before the ids existed) is not filtered.
    # A skipped invoice record always stores its payments, so an empty set there matches nothing.
    filter_by_document = not isinstance(source, AttendeeInvoice) or bool(on_document)
    if refund_ids:
        refunds = Refund.objects.filter(
            pk__in=refund_ids,
            status=Refund.RefundStatus.SUCCEEDED,
            payment__stripe_session_id=stripe_session_id,
        )
        if filter_by_document:
            refunds = refunds.filter(payment_id__in=on_document)
        refund_rows = list(refunds.select_related("payment__ticket__event", "payment__ticket__tier"))
        return (refund_rows, []) if refund_rows else None
    payments = Payment.objects.filter(id__in=refunded_payment_ids, stripe_session_id=stripe_session_id)
    if filter_by_document:
        payments = payments.filter(id__in=on_document)
    refunded_payments = list(payments.select_related("ticket__event", "ticket__tier"))
    return ([], refunded_payments) if refunded_payments else None


def record_skipped_credit_note(
    source: AttendeeInvoice | SkippedFiscalDocument,
    decision: Decision,
    refund_rows: list[Refund],
    refunded_payments: list[Payment],
) -> None:
    """Record the credit note a policy refused, once per refund (or legacy payment) set (#1091).

    Refunds already credited by a Revel credit note, or already in an earlier skipped
    record for ``source``, are left out: a retry records nothing twice, and a superset retry
    records only its new refunds.
    """
    with transaction.atomic():
        # Serialize concurrent retries on the document being corrected.
        source = type(source).objects.select_for_update().get(pk=source.pk)
        prior = list(source.skipped_credit_notes.prefetch_related("payments", "refunds"))
        existing_cns = (
            list(AttendeeInvoiceCreditNote.objects.filter(invoice=source).prefetch_related("payments", "refunds"))
            if isinstance(source, AttendeeInvoice)
            else []
        )
        # A record without refund rows (legacy path) covers its payments in full.
        covered_payment_ids = {p.id for doc in prior if not doc.refunds.all() for p in doc.payments.all()}
        build: AttendeeInvoiceCreditNote | _CreditNoteBuild | None = None
        # Both an invoice and a skipped invoice store payment-tagged lines; credits follow them.
        invoice_lines = source.line_items
        # A skipped invoice has no reverse_charge column; the checkout snapshot on its payments is the source.
        if isinstance(source, AttendeeInvoice):
            reverse_charge = source.reverse_charge
            out_of_scope = invoice_out_of_scope(source)
        else:
            sale_payments = [r.payment for r in refund_rows] or list(refunded_payments)
            snapshot = (sale_payments[0].buyer_billing_snapshot or {}) if sale_payments else {}
            reverse_charge = bool(snapshot.get("reverse_charge"))
            sale_event = sale_payments[0].ticket.event if sale_payments else None
            out_of_scope = out_of_scope_for(
                sale_event,
                sale_event.organization.vat_country_code if sale_event else "",
                snapshot.get("vat_country_code", ""),
            )
        if refund_rows:
            covered_refund_ids = {r.id for doc in prior for r in doc.refunds.all()}
            rows = [
                r for r in refund_rows if r.id not in covered_refund_ids and r.payment_id not in covered_payment_ids
            ]
            build = (
                _build_from_refund_rows(rows, existing_cns, reverse_charge, out_of_scope, invoice_lines, prior)
                if rows
                else None
            )
        else:
            covered_payment_ids |= {p.id for doc in prior for p in doc.payments.all()}
            payments = [p for p in refunded_payments if p.id not in covered_payment_ids]
            build = (
                _build_from_payments(payments, existing_cns, reverse_charge, out_of_scope, invoice_lines)
                if payments
                else None
            )
        if isinstance(build, _CreditNoteBuild):
            skipped_fiscal_document_service.record_skipped_credit_note(
                source=source,
                decision=decision,
                line_items=build.line_items,
                payments=build.payments,
                refunds=build.refund_rows,
            )


def record_credit_note_for_skipped_invoice(
    stripe_session_id: str, refunded_payment_ids: list[UUID], refund_ids: list[UUID] | None
) -> None:
    """A refund on a sale whose invoice was skipped: its credit note is skipped too, for the same reason."""
    parent = SkippedFiscalDocument.objects.filter(
        stripe_session_id=stripe_session_id, kind=SkippedFiscalDocument.Kind.INVOICE
    ).first()
    if parent is None:
        return
    loaded = load_refunded(parent, stripe_session_id, refunded_payment_ids, refund_ids)
    if loaded is None:
        return
    decision = Decision.block(parent.reason, code=parent.reason_code, country=parent.policy_country)
    record_skipped_credit_note(parent, decision, *loaded)
    logger.warning("attendee_credit_note_skipped_invoice_skipped", session_id=stripe_session_id)


def create_credit_note(
    invoice: AttendeeInvoice, refund_rows: list[Refund], refunded_payments: list[Payment], number_prefix: str
) -> tuple[AttendeeInvoiceCreditNote | None, bool]:
    """Create the credit note for ``refund_rows`` (or, when there are none, for whole ``refunded_payments``).

    Runs under a lock on the invoice, so concurrent requests for one invoice are serialized. A fully
    credited invoice becomes ``CANCELLED``. The note has no PDF yet: the caller renders it.

    Returns:
        ``(credit_note, created)``. The note is the existing one when the same refund (or legacy
        payment) set was credited before, and ``None`` when earlier notes cover each request.
    """
    with transaction.atomic():
        # Lock the invoice row to serialize concurrent credit note creation
        invoice = AttendeeInvoice.objects.select_for_update().get(pk=invoice.pk)

        # Collect existing credit notes so both idempotency paths below can
        # compare against what's already been credited.
        existing_cns = list(
            AttendeeInvoiceCreditNote.objects.filter(invoice=invoice).prefetch_related("payments", "refunds")
        )

        out_of_scope = invoice_out_of_scope(invoice)
        build = (
            _build_from_refund_rows(refund_rows, existing_cns, invoice.reverse_charge, out_of_scope, invoice.line_items)
            if refund_rows
            else _build_from_payments(
                refunded_payments, existing_cns, invoice.reverse_charge, out_of_scope, invoice.line_items
            )
        )
        if not isinstance(build, _CreditNoteBuild):
            return build, False

        credit_note_number = get_next_sequential_number(
            AttendeeInvoiceCreditNote, number_prefix, timezone.now().year, "credit_note_number"
        )

        # Figured by the invoice's method (#1234); the prior notes were read under this lock.
        totals = credit_note_totals(invoice, build.line_items, existing_cns)
        credit_note = AttendeeInvoiceCreditNote.objects.create(
            invoice=invoice,
            credit_note_number=credit_note_number,
            amount_gross=totals.amount_gross,
            amount_net=totals.amount_net,
            amount_vat=totals.amount_vat,
            rounding_amount=totals.rounding_amount,
            totals_method=totals.totals_method,
            line_items=totals.lines,
            issued_at=timezone.now(),
            issuer_name=platform_issuer_name(),  # #1258
        )
        credit_note.payments.set(build.payments)
        if build.refund_rows is not None:
            credit_note.refunds.set(build.refund_rows)

        # Check if fully credited → mark invoice as CANCELLED
        total_credited = sum(cn.amount_gross for cn in invoice.credit_notes.all())
        if total_credited >= invoice.total_gross:
            invoice.status = AttendeeInvoice.InvoiceStatus.CANCELLED
            invoice.save(update_fields=["status", "updated_at"])
    return credit_note, True
