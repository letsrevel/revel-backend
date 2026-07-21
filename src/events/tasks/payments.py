"""Celery tasks for payment expiry and ticket file-cache cleanup."""

import typing as t
from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID

import structlog
from celery import shared_task
from django.db import transaction
from django.db.models import F, Max, Q, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from events.models import HeldSeriesPass, Payment, Ticket, TicketTier

logger = structlog.get_logger(__name__)

# How long a Payment implicated in a recorded money-correctness incident
# (Payment.incident_hold_at set by hold_mismatch_payments, #756) is exempt from
# the expiry sweep. Bounded on purpose: an unresolved hold must not make the row
# — or the tier capacity its PENDING ticket still occupies — immortal. Sized for
# a paged, alert-driven investigation (the alert fires on the first occurrence),
# with an order of magnitude of slack.
INCIDENT_HOLD_RETENTION = timedelta(days=30)


@shared_task(name="events.hold_mismatch_payments")
def hold_mismatch_payments(payment_ids: list[str]) -> int:
    """Stamp an incident hold on payments implicated in a session-total mismatch (#756).

    Dispatched (bare ``.delay()``) by ``stripe_incidents.record_session_total_mismatch``:
    the webhook detection point raises on purpose so its transaction rolls back, which
    means the hold cannot be written synchronously from the request — the broker message
    is what survives the rollback (the dispatch-then-raise pattern in
    docs/engineering-notes.md). The rows themselves pre-exist the doomed request (created
    at reserve time), so they are here to be stamped.

    Idempotent, first detection wins: a Stripe redelivery re-records the same incident,
    and restamping would restart the retention clock — only rows still without a hold
    are touched.

    Args:
        payment_ids: The Payment pks implicated in the recorded mismatch.

    Returns:
        The number of rows newly placed under hold.
    """
    held = Payment.objects.filter(pk__in=payment_ids, incident_hold_at__isnull=True).update(
        incident_hold_at=timezone.now()
    )
    logger.info("incident_hold_applied", payment_ids=payment_ids, newly_held=held)
    return held


@shared_task(name="events.cleanup_expired_payments")
def cleanup_expired_payments() -> int:
    """Finds and deletes expired payments that are still in a 'pending' state.

    Releases their associated ticket reservation by decrementing the tier's
    quantity_sold counter, and cancels any series pass stranded by the expired
    checkout (releasing its quantity_sold too).
    This task is idempotent and safe to run periodically.

    Incident holds (#756): a PENDING payment implicated in a recorded
    ``stripe_session_total_mismatch`` carries ``incident_hold_at`` — its row (and
    its ticket) IS the evidence an operator reconciles against, so the sweep
    retains it. The hold is bounded, not an exclusion: the row leaves either when
    an operator clears the hold after resolving the incident (reclaimed on the
    next run, on the normal path) or when ``INCIDENT_HOLD_RETENTION`` lapses.
    """
    # Imported here: events.tasks.__init__ imports this module while
    # series_pass_service itself imports events.tasks (materialization task).
    from events.service.series_pass_service import expire_held_passes_for_tickets

    now = timezone.now()
    # Index-friendly: the first branch is the existing expires_at index scan
    # (incident_hold_at IS NULL merely filters it); the second is a range scan on
    # the near-empty partial index payment_incident_hold_idx. Postgres can
    # BitmapOr the two.
    reclaimable = Q(status=Payment.PaymentStatus.PENDING) & (
        Q(incident_hold_at__isnull=True, expires_at__lt=now) | Q(incident_hold_at__lt=now - INCIDENT_HOLD_RETENTION)
    )

    # Candidate payment IDs only — re-filtered by the same predicate and locked
    # inside the transaction below. Computing the release-set outside the transaction
    # (the old approach) let a concurrent reclaim on the same rows (cancel_pending_checkout,
    # the payment_intent.canceled webhook) double-decrement a tier: both routes would
    # count the same still-outside-tx-computed payment, since neither re-checked
    # PENDING against the other's already-committed change (#632).
    candidate_payment_ids = list(Payment.objects.filter(reclaimable).values_list("id", flat=True))

    if not candidate_payment_ids:
        return 0

    with transaction.atomic():
        # Re-apply the reclaimable predicate *and* lock the rows: only payments still
        # PENDING (and still not incident-held — a mismatch can be recorded between
        # the snapshot above and this lock) are ours to reclaim, and select_for_update
        # serializes a concurrent cancel_pending_checkout/webhook reclaim on the
        # same rows instead of racing it. The decrement count, the payments
        # deleted, and the tickets deleted all come from this same in-transaction,
        # locked, still-reclaimable set (#632).
        locked_payments = list(
            Payment.objects.select_for_update()
            .filter(reclaimable, pk__in=candidate_payment_ids)
            .select_related("ticket", "ticket__tier")
        )
        if not locked_payments:
            return 0

        payment_ids_to_delete = [payment.id for payment in locked_payments]
        ticket_ids_to_delete = [payment.ticket_id for payment in locked_payments]
        # Only count payments whose ticket is still PENDING. A ticket cancelled via
        # POST /tickets/{id}/cancel (cancellation_service._finalize_cancellation)
        # already decremented quantity_sold at cancel time and may leave its Payment
        # PENDING forever (no stripe_payment_intent_id -> no refund path ever touches
        # it) — counting it here too would release the tier slot a second time (#632).
        tickets_to_release_by_tier: Counter[UUID] = Counter(
            payment.ticket.tier_id
            for payment in locked_payments
            if payment.ticket.tier_id is not None and payment.ticket.status == Ticket.TicketStatus.PENDING
        )

        logger.info(
            f"Found {len(payment_ids_to_delete)} expired payments to clean up "
            f"across {len(tickets_to_release_by_tier)} tiers."
        )

        # Cancel any series pass whose checkout these payments belonged to first
        # (releasing SeriesPass.quantity_sold so the buyer can purchase again),
        # THEN release tier capacity — pass row before tier rows, matching
        # SeriesPassPurchaseService.purchase's lock order to avoid deadlocking
        # against a concurrent purchase on the same pass. Ticket-based (not
        # session-based): a reserved-but-not-sessioned series pass's
        # held_pass.stripe_session_id is "" and would be missed by a session
        # lookup (#632).
        expire_held_passes_for_tickets(ticket_ids_to_delete)

        # Atomically decrement the quantity_sold for each affected tier, floored at
        # zero as defense-in-depth (mirrors pending_checkout._release_batch_tier_capacity) —
        # the in-tx recompute above is what actually prevents the double-decrement.
        for tier_id, count_to_release in tickets_to_release_by_tier.items():
            TicketTier.objects.select_for_update().filter(pk=tier_id).update(
                quantity_sold=Greatest(F("quantity_sold") - count_to_release, Value(0))
            )

        # Delete payments first due to PROTECT constraint on Ticket
        Payment.objects.filter(pk__in=payment_ids_to_delete).delete()

        # Now delete the associated pending tickets
        Ticket.objects.filter(pk__in=ticket_ids_to_delete, status=Ticket.TicketStatus.PENDING).delete()

    logger.info(f"Successfully cleaned up {len(payment_ids_to_delete)} expired payments.")
    return len(payment_ids_to_delete)


class TicketFileCacheCleanupResult(t.TypedDict):
    """Telemetry counters returned by ``cleanup_ticket_file_cache``."""

    cleaned: int


def _sweep_ticket_files(now: datetime) -> list[UUID]:
    """Delete cached PDF/pkpass files for tickets whose events have ended.

    Returns:
        The pks of tickets whose cached files were cleared.
    """
    tickets_with_files = Ticket.objects.filter(event__end__lt=now).filter(Q(pdf_file__gt="") | Q(pkpass_file__gt=""))

    cleaned_pks: list[UUID] = []
    for ticket in tickets_with_files.only("pk", "pdf_file", "pkpass_file"):
        try:
            if ticket.pdf_file:
                ticket.pdf_file.delete(save=False)
            if ticket.pkpass_file:
                ticket.pkpass_file.delete(save=False)
            cleaned_pks.append(ticket.pk)
        except OSError:
            logger.warning("Failed to clean cached files for ticket %s", ticket.pk, exc_info=True)

    if cleaned_pks:
        Ticket.objects.filter(pk__in=cleaned_pks).update(
            pdf_file="",
            pkpass_file="",
            file_content_hash=None,
        )
        logger.info("cleanup_ticket_file_cache_done", cleaned=len(cleaned_pks))

    return cleaned_pks


def _sweep_series_pass_files(now: datetime) -> list[UUID]:
    """Delete cached PDF/pkpass files for held series passes whose covered events have all ended.

    Gated by the LAST covered event's end (``Max("series_pass__tier_links__event__end")``),
    mirroring the per-ticket ``event__end`` cutoff — a pass still covering an upcoming event
    stays downloadable even if it also covers past ones.

    Returns:
        The pks of held passes whose cached files were cleared.
    """
    passes_with_files = (
        HeldSeriesPass.objects.annotate(last_event_end=Max("series_pass__tier_links__event__end"))
        .filter(last_event_end__lt=now)
        .filter(Q(pdf_file__gt="") | Q(pkpass_file__gt=""))
    )

    cleaned_pks: list[UUID] = []
    for held_pass in passes_with_files.only("pk", "pdf_file", "pkpass_file"):
        try:
            if held_pass.pdf_file:
                held_pass.pdf_file.delete(save=False)
            if held_pass.pkpass_file:
                held_pass.pkpass_file.delete(save=False)
            cleaned_pks.append(held_pass.pk)
        except OSError:
            logger.warning("Failed to clean cached files for series pass %s", held_pass.pk, exc_info=True)

    if cleaned_pks:
        HeldSeriesPass.objects.filter(pk__in=cleaned_pks).update(
            pdf_file="",
            pkpass_file="",
            file_content_hash=None,
        )
        logger.info("cleanup_series_pass_file_cache_done", cleaned=len(cleaned_pks))

    return cleaned_pks


@shared_task(name="events.cleanup_ticket_file_cache")
def cleanup_ticket_file_cache() -> TicketFileCacheCleanupResult:
    """Delete cached PDF/pkpass files for tickets and series passes whose events have ended.

    Frees storage for past events since cached files are no longer needed. Files can
    always be regenerated on demand if needed.

    Returns:
        Dict with the total count of tickets and held passes cleaned.
    """
    now = timezone.now()
    cleaned_ticket_pks = _sweep_ticket_files(now)
    cleaned_pass_pks = _sweep_series_pass_files(now)
    return {"cleaned": len(cleaned_ticket_pks) + len(cleaned_pass_pks)}
