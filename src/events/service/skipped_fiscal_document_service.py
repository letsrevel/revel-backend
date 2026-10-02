"""Skipped fiscal documents: recording, listing and resolving (#1091).

A row is written whenever a country policy makes Revel skip an attendee invoice or
credit note (see :mod:`events.models.skipped_fiscal_document`). The generation code in
:mod:`events.service.attendee_invoice_service` decides *when*; this module only
snapshots and stores, so it must not import that module (it imports this one).
"""

import typing as t
from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from common.utils import get_or_create_with_race_protection
from events.compliance import Decision
from events.models import AttendeeInvoice, Organization, Payment, Refund, SkippedFiscalDocument, Ticket
from events.models.attendee_invoice import InvoiceLineItemDict, vat_breakdown_of

if t.TYPE_CHECKING:
    from accounts.models import RevelUser


class _Totals(t.TypedDict):
    total_gross: Decimal
    total_net: Decimal
    total_vat: Decimal


def _totals(line_items: list[InvoiceLineItemDict]) -> _Totals:
    """Header totals summed from the line items, so they always match ``vat_breakdown``."""
    buckets = vat_breakdown_of(line_items)
    zero = Decimal("0.00")
    return _Totals(
        total_gross=sum((b["gross_amount"] for b in buckets), zero),
        total_net=sum((b["net_amount"] for b in buckets), zero),
        total_vat=sum((b["vat_amount"] for b in buckets), zero),
    )


def _decision_fields(decision: Decision) -> dict[str, str]:
    return {
        "reason_code": SkippedFiscalDocument.ReasonCode(decision.code),
        "policy_country": decision.country,
        "reason": decision.reason,
    }


def record_skipped_invoice(
    payments: list[Payment], line_items: list[InvoiceLineItemDict], decision: Decision
) -> SkippedFiscalDocument:
    """Record the attendee invoice a policy refused for one checkout session; idempotent per session.

    Args:
        payments: The session's succeeded payments, with ``ticket__event__organization`` selected.
        line_items: The lines the invoice would have carried.
        decision: The refusal (with ``code`` and ``country``).

    Returns:
        The new record, or the one already stored for the session.
    """
    first = payments[0]
    snapshot: t.Mapping[str, t.Any] = first.buyer_billing_snapshot or {}
    defaults: dict[str, t.Any] = {
        "kind": SkippedFiscalDocument.Kind.INVOICE,
        **_decision_fields(decision),
        "organization": first.ticket.event.organization,
        "event": first.ticket.event,
        "user": first.user,
        "stripe_session_id": first.stripe_session_id,
        "buyer_name": snapshot.get("billing_name", ""),
        "buyer_email": snapshot.get("billing_email", ""),
        "buyer_vat_id": snapshot.get("vat_id", ""),
        "buyer_vat_country": snapshot.get("vat_country_code", ""),
        "buyer_address": snapshot.get("billing_address", ""),
        "buyer_vat_id_status": snapshot.get("vat_id_status", ""),
        "currency": first.currency,
        "line_items": line_items,
        **_totals(line_items),
    }
    with transaction.atomic():
        doc, created = get_or_create_with_race_protection(
            SkippedFiscalDocument,
            Q(stripe_session_id=first.stripe_session_id, kind=SkippedFiscalDocument.Kind.INVOICE),
            defaults,
        )
        if created:
            doc.payments.set(payments)
    return doc


def record_skipped_credit_note(
    *,
    source: AttendeeInvoice | SkippedFiscalDocument,
    decision: Decision,
    line_items: list[InvoiceLineItemDict],
    payments: t.Collection[Payment],
    refunds: list[Refund] | None,
) -> SkippedFiscalDocument:
    """Record a credit note a policy refused, for refunds not covered by an earlier record.

    The caller filters out refunds already credited or recorded, under a lock on ``source``.

    Args:
        source: The Revel invoice the credit note would correct, or the skipped invoice record.
        decision: The refusal (with ``code`` and ``country``).
        line_items: The lines the credit note would have carried.
        payments: The refunded payments it covers.
        refunds: The refund rows it covers; ``None`` on the legacy payment-keyed path.
    """
    doc = SkippedFiscalDocument.objects.create(
        kind=SkippedFiscalDocument.Kind.CREDIT_NOTE,
        **_decision_fields(decision),
        organization=source.organization,
        event=source.event,
        user=source.user,
        stripe_session_id=source.stripe_session_id,
        invoice=source if isinstance(source, AttendeeInvoice) else None,
        parent=source if isinstance(source, SkippedFiscalDocument) else None,
        buyer_name=source.buyer_name,
        buyer_email=source.buyer_email,
        buyer_vat_id=source.buyer_vat_id,
        buyer_vat_country=source.buyer_vat_country,
        buyer_address=source.buyer_address,
        buyer_vat_id_status=getattr(source, "buyer_vat_id_status", ""),
        currency=source.currency,
        line_items=line_items,
        **_totals(line_items),
    )
    doc.payments.set(payments)
    if refunds:
        doc.refunds.set(refunds)
    return doc


def _skipped_invoices() -> QuerySet[SkippedFiscalDocument]:
    return SkippedFiscalDocument.objects.filter(kind=SkippedFiscalDocument.Kind.INVOICE)


def invoice_skipped_annotation() -> Exists:
    """Per ticket: whether its sale's attendee invoice was skipped. Annotate as ``invoice_skipped``."""
    return Exists(_skipped_invoices().filter(payments__ticket_id=OuterRef("pk")))


def ticket_invoice_skipped(ticket: Ticket) -> bool:
    """Whether the ticket's attendee invoice was skipped; reads the annotation when present."""
    annotated: bool | None = getattr(ticket, "invoice_skipped", None)
    if annotated is not None:
        return annotated
    return _skipped_invoices().filter(payments__ticket_id=ticket.pk).exists()


def list_skipped_documents(organization: Organization) -> QuerySet[SkippedFiscalDocument]:
    """The organization's skipped documents, newest first, with what the list schema reads."""
    return (
        SkippedFiscalDocument.objects.filter(organization=organization)
        .select_related("event", "invoice")
        .prefetch_related("payments")
        .order_by("-decided_at", "-id")
    )


def resolve_skipped_document(
    doc: SkippedFiscalDocument, user: "RevelUser", external_reference: str
) -> SkippedFiscalDocument:
    """Mark a skipped document as issued in the organizer's own system (re-resolving updates the reference)."""
    doc.resolved_at = timezone.now()
    doc.resolved_by = user
    doc.external_reference = external_reference
    doc.save(update_fields=["resolved_at", "resolved_by", "external_reference", "updated_at"])
    return doc
