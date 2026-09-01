"""Draft attendee-invoice lifecycle: editing, issuing, deletion.

Split from ``attendee_invoice_service`` when that module reached the 1000-line
cap (#911). The seam is the draft lifecycle: everything here acts on an invoice
that already exists, where generation, PDF rendering and delivery live next
door. The only thing it needs from there is :func:`_generate_and_save_pdf`,
which stays put so the render_pdf patch target every test uses is unchanged.
"""

import typing as t
from decimal import Decimal

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models.attendee_invoice import (
    AttendeeInvoice,
    DerivedTotals,
    InvoiceLineItemDict,
    SubmittedLineItem,
)
from events.service.attendee_invoice_service import _generate_and_save_pdf

logger = structlog.get_logger(__name__)


EDITABLE_DRAFT_FIELDS = frozenset(
    {
        "buyer_name",
        "buyer_vat_id",
        "buyer_vat_country",
        "buyer_address",
        "buyer_email",
        "currency",
        "reverse_charge",
        "discount_code_text",
        "line_items",
    }
)

# Money columns derived from ``line_items``, never independently writable (#911).
# They used to sit in EDITABLE_DRAFT_FIELDS, so a PATCH of ``total_gross`` alone left
# a header claiming EUR 1000 over line items summing to EUR 111 -- a contradiction
# that shipped in one response once ``vat_breakdown`` was exposed (#910), and that
# issuance turned into a delivered PDF. Recomputed from the lines instead.
DERIVED_TOTAL_FIELDS = ("total_gross", "total_net", "total_vat", "vat_rate", "discount_amount_total")


# Optional string columns (blank=True, default="") whose schema fields are nullable:
# the FE sends `null` for a cleared optional field, so coerce None -> "" to respect
# the NOT NULL DB constraint. Only fields that are genuinely blankable belong here —
# required columns (e.g. buyer_name, currency) lack blank=True, so an empty string is
# not a valid value for them. This set doubles as the exemption list for the null guard
# in update_draft_invoice (#908): a null on any field outside it is rejected with a 422
# naming that field, before full_clean() is ever reached.
_BLANKABLE_STRING_FIELDS = frozenset(
    {
        "buyer_vat_id",
        "buyer_vat_country",
        "buyer_address",
        "buyer_email",
        "discount_code_text",
    }
)


def _normalize_line_items(line_items: list[SubmittedLineItem]) -> list[InvoiceLineItemDict]:
    """Coerce submitted line items to the stored shape: every key present, every amount a string.

    The schema parses amounts as ``Decimal``; the JSON column stores strings, and
    both :func:`_derive_totals` and ``vat_breakdown`` read them back that way.
    """
    return [
        InvoiceLineItemDict(
            description=item.get("description", ""),
            unit_price_gross=str(item.get("unit_price_gross", "0.00")),
            discount_amount=str(item.get("discount_amount", "0.00")),
            net_amount=str(item.get("net_amount", "0.00")),
            vat_amount=str(item.get("vat_amount", "0.00")),
            vat_rate=str(item.get("vat_rate", "0.00")),
        )
        for item in line_items
    ]


def _derive_totals(line_items: list[InvoiceLineItemDict]) -> DerivedTotals:
    """Compute an invoice's header money columns from its line items.

    Mirrors :func:`generate_attendee_invoice`, which sums the same per-payment
    amounts :func:`_build_line_items` writes into each line -- so a recomputed header
    is identical to a freshly generated one, no rounding or tolerance involved.
    ``vat_rate`` inherits its lossy "first payment's rate" convention; consult
    ``has_mixed_vat_rates`` before rendering it.

    Reads the four keys ``vat_breakdown`` reads, so a row malformed enough to break
    one breaks both. ``discount_amount`` -- the one key the breakdown never touches
    -- is read defensively: this runs in the issuance guard on rows nobody
    normalized, where a missing key would be a 500 on the document it must judge.
    """
    zero = Decimal("0.00")
    return DerivedTotals(
        total_gross=sum((Decimal(item["unit_price_gross"]) for item in line_items), zero),
        total_net=sum((Decimal(item["net_amount"]) for item in line_items), zero),
        total_vat=sum((Decimal(item["vat_amount"]) for item in line_items), zero),
        discount_amount_total=sum((Decimal(item.get("discount_amount", "0.00")) for item in line_items), zero),
        vat_rate=Decimal(line_items[0]["vat_rate"]) if line_items else zero,
    )


def _assert_totals_reconcile(invoice: AttendeeInvoice) -> None:
    """Refuse to issue an invoice whose header totals disagree with its line items.

    :func:`update_draft_invoice` derives the header from the lines, so a draft
    edited through the API always reconciles. This guards the other door (#911): a
    row that drifted before that landed, or one desynced by a raw
    ``QuerySet.update()`` or shell write, must not become a legal document on the
    strength of a header its own ``vat_breakdown`` contradicts. The Django admin is
    *not* such a door -- ``AttendeeInvoiceAdmin`` has every money column and
    ``line_items`` read-only -- so revisit this before relaxing that admin.
    Recovery is a PATCH of ``line_items``, which recomputes the header.

    Only the three figures a buyer is billed on are checked: ``vat_rate`` is a lossy
    scalar guarded by ``has_mixed_vat_rates`` and ``discount_amount_total`` is
    informational, so refusing a correct invoice over either would be disproportionate.

    Raises:
        HttpError 422: If any reconciled total differs from its line-item sum.
    """
    derived = _derive_totals(invoice.line_items)
    mismatched = [
        # Symbolic, not prose: the message around it is translated, this is not.
        f"{name} ({actual} != {expected})"
        for name, actual, expected in (
            ("total_gross", invoice.total_gross, derived["total_gross"]),
            ("total_net", invoice.total_net, derived["total_net"]),
            ("total_vat", invoice.total_vat, derived["total_vat"]),
        )
        if actual != expected
    ]
    if mismatched:
        raise HttpError(
            422,
            str(_("Invoice totals do not reconcile with its line items: {details}")).format(
                details="; ".join(mismatched)
            ),
        )


def _apply_draft_update(invoice: AttendeeInvoice, update_data: dict[str, t.Any]) -> AttendeeInvoice:
    """Write an already-validated payload onto the invoice, under a row lock.

    Re-reads with ``select_for_update`` and re-checks DRAFT: the caller's instance was
    fetched before any validation ran, so its status is a snapshot, and a concurrent
    issue landing in that window would leave an unlocked write editing an
    already-issued legal document. Split out so the lock covers the write and nothing else.

    Returns:
        The locked, updated invoice -- not the instance passed in.

    Raises:
        HttpError 409: If the locked row is no longer a draft.
    """
    with transaction.atomic():
        invoice = AttendeeInvoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.status != AttendeeInvoice.InvoiceStatus.DRAFT:
            raise HttpError(409, str(_("Only draft invoices can be edited.")))

        for field, value in update_data.items():
            setattr(invoice, field, value)

        # Invalidate stale PDF so it gets regenerated on next download
        if invoice.pdf_file:
            invoice.pdf_file.delete(save=False)
            update_data["pdf_file"] = ""

        invoice.save(update_fields=[*update_data.keys(), "updated_at"])
        return invoice


def update_draft_invoice(
    invoice: AttendeeInvoice,
    update_data: dict[str, t.Any],
) -> AttendeeInvoice:
    """Update a DRAFT invoice. Only drafts are editable.

    Editing ``line_items`` recomputes every :data:`DERIVED_TOTAL_FIELDS` column from
    the new lines: an organizer wanting a different total states it as line items,
    which is what an invoice is. The write happens in :func:`_apply_draft_update`
    under a row lock, so the status is checked twice -- here to fail fast, and there
    because only the locked row is current.

    Args:
        invoice: The invoice to update.
        update_data: Dict of fields to update (from exclude_unset).

    Returns:
        The updated invoice, re-read under the lock -- not the instance passed in.

    Raises:
        HttpError 409: If the invoice is not a draft, as fetched or once locked.
        HttpError 422: If update_data contains disallowed fields (including a
            derived total), or an explicit null for a field that is not one of
            the blankable optional strings.
    """
    if invoice.status != AttendeeInvoice.InvoiceStatus.DRAFT:
        raise HttpError(409, str(_("Only draft invoices can be edited.")))

    if not update_data:
        return invoice

    disallowed = set(update_data.keys()) - EDITABLE_DRAFT_FIELDS
    if disallowed:
        raise HttpError(422, str(_("Cannot edit fields: {fields}")).format(fields=", ".join(sorted(disallowed))))

    nulled = {field for field, value in update_data.items() if value is None} - _BLANKABLE_STRING_FIELDS
    if nulled:
        raise HttpError(422, str(_("Fields cannot be null: {fields}")).format(fields=", ".join(sorted(nulled))))

    if "line_items" in update_data:
        update_data["line_items"] = _normalize_line_items(update_data["line_items"])
        # Recomputed only on an edit that touches the lines: doing it
        # unconditionally would zero the totals of any row with empty line_items.
        update_data.update(_derive_totals(update_data["line_items"]))

    # Coerce None -> "" for blankable string columns (FE sends null for cleared fields).
    for field in _BLANKABLE_STRING_FIELDS:
        if field in update_data and update_data[field] is None:
            update_data[field] = ""

    return _apply_draft_update(invoice, update_data)


def issue_draft_invoice(invoice: AttendeeInvoice) -> AttendeeInvoice:
    """Issue a DRAFT invoice: finalize, set issued_at, regenerate PDF.

    Idempotent: if the invoice is already ISSUED (e.g., PDF generation failed
    on a previous attempt), re-generates the PDF and re-delivers.

    Args:
        invoice: The draft or already-issued invoice.

    Returns:
        The issued invoice, re-read under the lock -- not the instance passed in.

    Raises:
        HttpError 409: If the invoice is CANCELLED.
        HttpError 422: If a DRAFT's header totals do not reconcile with its line
            items -- such an invoice must not become a legal document (#911).
    """
    with transaction.atomic():
        # Lock and re-read: the reconciliation below decides whether this row may
        # become a legal document, so it must judge the committed row. Unlocked, a
        # PATCH landing after the controller's fetch was validated in absentia and
        # then rendered into a PDF contradicting what the database holds.
        invoice = AttendeeInvoice.objects.select_for_update().get(pk=invoice.pk)

        if invoice.status == AttendeeInvoice.InvoiceStatus.CANCELLED:
            raise HttpError(409, str(_("Cancelled invoices cannot be issued.")))

        if invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT:
            # Only on the DRAFT -> ISSUED transition: re-issuing an already-ISSUED
            # invoice exists to recover a failed PDF generation, and that recovery
            # must stay available even for a row that drifted before #911 landed.
            _assert_totals_reconcile(invoice)
            invoice.status = AttendeeInvoice.InvoiceStatus.ISSUED
            invoice.issued_at = timezone.now()
            invoice.save(update_fields=["status", "issued_at", "updated_at"])

    # (Re-)generate PDF without DRAFT watermark. Outside the lock: WeasyPrint is
    # slow, and once the status commits no edit can land -- update_draft_invoice
    # refuses a non-DRAFT row under that same lock.
    _generate_and_save_pdf(invoice)

    return invoice


def issue_and_deliver(invoice: AttendeeInvoice) -> AttendeeInvoice:
    """Issue a DRAFT invoice and enqueue background delivery to the buyer.

    Thin orchestration helper for the organization admin "issue" endpoint:
    finalizes the invoice via :func:`issue_draft_invoice` and then schedules
    the delivery task. Keeps celery dispatch out of the controller layer.

    Args:
        invoice: The draft or already-issued invoice.

    Returns:
        The issued invoice.
    """
    from events.tasks import deliver_attendee_invoice_task

    invoice = issue_draft_invoice(invoice)
    # Defer delivery until the issuance write commits so the worker reads
    # the issued invoice. With ATOMIC_REQUESTS=True an immediate .delay()
    # would race the request commit.
    transaction.on_commit(lambda: deliver_attendee_invoice_task.delay(str(invoice.id)))
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
