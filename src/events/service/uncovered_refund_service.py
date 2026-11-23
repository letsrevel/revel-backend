"""Refunds that reached a sale before its invoice did (#1484).

The credit-note task for a refund returns when the sale has no invoice yet, and nothing retries it.
The invoice task calls :func:`uncovered_refunds` after it runs, so the refunds that no credit note
covers yet are credited in whatever order the two tasks ran.
"""

from uuid import UUID

from events.models import Refund, SkippedFiscalDocument
from events.models.attendee_invoice import AttendeeInvoice
from events.service.attendee_credit_note_service import invoiced_payment_ids


def uncovered_refunds(stripe_session_id: str) -> tuple[list[UUID], list[UUID]]:
    """The payments and refund rows of the session's invoiced sale that no credit note covers yet.

    Only payments on the invoice (its lines' ``payment_id``), or on the skipped invoice record, count:
    a payment that was fully refunded before the invoice was generated is not on the invoice, so it
    needs no credit note. A refund already in a credit note or in a skipped credit-note record is left
    out; the credit-note generation is idempotent on refund ids for the rest.

    Args:
        stripe_session_id: The Stripe checkout session ID of the sale.

    Returns:
        ``(payment_ids, refund_ids)``, both empty when there is nothing to credit.
    """
    source: AttendeeInvoice | SkippedFiscalDocument | None = (
        AttendeeInvoice.objects.filter(stripe_session_id=stripe_session_id).only("line_items").first()
        or SkippedFiscalDocument.objects.filter(
            stripe_session_id=stripe_session_id, kind=SkippedFiscalDocument.Kind.INVOICE
        ).first()
    )
    if source is None:
        return [], []
    payment_ids = invoiced_payment_ids(source)
    if not payment_ids:
        return [], []
    rows = list(
        Refund.objects.filter(
            payment_id__in=payment_ids,
            payment__stripe_session_id=stripe_session_id,
            status=Refund.RefundStatus.SUCCEEDED,
            credit_notes__isnull=True,
            skipped_fiscal_documents__isnull=True,
        )
        .order_by("pk")
        .values_list("pk", "payment_id")
        .distinct()
    )
    return sorted({payment_id for _, payment_id in rows}), [pk for pk, _ in rows]
