"""Skipped fiscal documents: recording, listing, resolving (#1091) and the daily digest (#1073).

A row is written whenever a country policy makes Revel skip an attendee invoice or
credit note (see :mod:`events.models.skipped_fiscal_document`). The generation code in
:mod:`events.service.attendee_invoice_service` decides *when*; this module only
snapshots and stores, so it must not import that module (it imports this one).
"""

import typing as t
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from common.models import SiteSettings
from common.utils import get_or_create_with_race_protection
from events.compliance import Decision
from events.models import AttendeeInvoice, Organization, Payment, Refund, SkippedFiscalDocument, Ticket
from events.models.attendee_invoice import InvoiceLineItemDict, vat_breakdown_of
from notifications.context_schemas import FiscalDocumentSkippedContext, SkippedFiscalDocumentItem
from notifications.enums import NotificationType

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


# ---------------------------------------------------------------------------
# Daily digest to the organizer (#1073)
# ---------------------------------------------------------------------------

# ponytail: the digest lists the most recent documents only; the link opens the full list.
_DIGEST_ITEMS = 10


def notify_skipped_documents() -> int:
    """Send each organization one digest of the documents skipped since its last one.

    For the daily beat task. Rows with ``notified_at`` unset are claimed under a row
    lock (``skip_locked``, so an overlapping run skips them), notified and stamped in
    the same transaction: a failure rolls the stamp back, and the notifications'
    dispatch waits for the commit (``notification_requested`` uses ``on_commit``).
    Rows whose organization is gone are never notified.

    Returns:
        The number of organizations notified.
    """
    org_ids = list(
        SkippedFiscalDocument.objects.filter(notified_at__isnull=True, organization__isnull=False)
        .values_list("organization_id", flat=True)
        .distinct()
    )
    notified = 0
    for org_id in org_ids:
        with transaction.atomic():
            docs = list(
                SkippedFiscalDocument.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("event")
                .filter(organization_id=org_id, notified_at__isnull=True)
                .order_by("-decided_at", "-id")
            )
            if not docs:
                continue
            _send_digest(Organization.objects.get(pk=org_id), docs)
            SkippedFiscalDocument.objects.filter(pk__in=[doc.pk for doc in docs]).update(notified_at=timezone.now())
        notified += 1
    return notified


def _send_digest(organization: Organization, docs: list[SkippedFiscalDocument]) -> None:
    """Notify the owner (linked to the skipped list) and ``manage_tickets`` staff (linked to the tickets)."""
    from notifications.service.eligibility import get_staff_for_notification
    from notifications.service.notification_helpers import notify_org_staff

    base = SiteSettings.get_solo().frontend_base_url
    admin = f"{base}/org/{organization.slug}/admin"
    items = [_digest_item(doc, admin) for doc in docs[:_DIGEST_ITEMS]]
    gross: dict[str, Decimal] = defaultdict(Decimal)
    for doc in docs:
        gross[doc.currency] += doc.total_gross
    invoices = sum(doc.kind == SkippedFiscalDocument.Kind.INVOICE for doc in docs)

    def context(is_owner: bool, action_url: str) -> FiscalDocumentSkippedContext:
        return FiscalDocumentSkippedContext(
            organization_id=str(organization.id),
            organization_name=organization.name,
            document_count=len(docs),
            invoice_count=invoices,
            credit_note_count=len(docs) - invoices,
            totals=[f"{currency} {amount:.2f}" for currency, amount in sorted(gross.items())],
            items=items,
            more_count=len(docs) - len(items),
            is_owner=is_owner,
            action_url=action_url,
        )

    recipients = list(get_staff_for_notification(organization.id, NotificationType.FISCAL_DOCUMENT_SKIPPED))
    owners = [user for user in recipients if user.id == organization.owner_id]
    staff = [user for user in recipients if user.id != organization.owner_id]
    for group, ctx in (
        (owners, context(True, f"{admin}/billing/skipped-documents")),
        (staff, context(False, items[0]["tickets_url"])),
    ):
        notify_org_staff(
            organization_id=organization.id,
            notification_type=NotificationType.FISCAL_DOCUMENT_SKIPPED,
            context=ctx,
            sender=notify_skipped_documents,
            recipients=group,
        )


def _digest_item(doc: SkippedFiscalDocument, admin_url: str) -> SkippedFiscalDocumentItem:
    """One digest line; invoices link to the event's tickets filtered on ``invoice_skipped``."""
    if doc.event_id is None:
        tickets_url = f"{admin_url}/events"
    else:
        tickets_url = f"{admin_url}/events/{doc.event_id}/tickets"
        if doc.kind == SkippedFiscalDocument.Kind.INVOICE:
            tickets_url += "?invoice_skipped=true"
    return SkippedFiscalDocumentItem(
        kind=doc.kind,
        event_name=doc.event.name if doc.event else "",
        buyer_name=doc.buyer_name,
        buyer_vat_id=doc.buyer_vat_id,
        amount=f"{doc.currency} {doc.total_gross:.2f}",
        policy_country=doc.policy_country,
        tickets_url=tickets_url,
    )
