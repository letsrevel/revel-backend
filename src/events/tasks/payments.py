"""Celery tasks for payment expiry and ticket file-cache cleanup."""

import typing as t
from collections import Counter
from datetime import datetime
from uuid import UUID

import structlog
from celery import shared_task
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from events.models import HeldSeriesPass, Payment, Ticket, TicketTier

logger = structlog.get_logger(__name__)


@shared_task(name="events.cleanup_expired_payments")
def cleanup_expired_payments() -> int:
    """Finds and deletes expired payments that are still in a 'pending' state.

    Releases their associated ticket reservation by decrementing the tier's
    quantity_sold counter, and cancels any series pass stranded by the expired
    checkout (releasing its quantity_sold too).
    This task is idempotent and safe to run periodically.
    """
    # Imported here: events.tasks.__init__ imports this module while
    # series_pass_service itself imports events.tasks (materialization task).
    from events.service.series_pass_service import expire_stranded_held_passes

    # Find payments for tickets that are still pending and whose Stripe session has expired.
    expired_payments_qs = Payment.objects.filter(
        status=Payment.PaymentStatus.PENDING, expires_at__lt=timezone.now()
    ).select_related("ticket", "ticket__tier")

    if not expired_payments_qs.exists():
        return 0

    # Collect IDs and tier counts before the transaction to avoid holding locks for too long
    payment_ids_to_delete = list(expired_payments_qs.values_list("id", flat=True))
    ticket_ids_to_delete = list(expired_payments_qs.values_list("ticket_id", flat=True))
    expired_session_ids = set(expired_payments_qs.values_list("stripe_session_id", flat=True))
    tickets_to_release_by_tier: Counter[UUID] = Counter(
        expired_payments_qs.filter(ticket__tier_id__isnull=False).values_list("ticket__tier_id", flat=True)
    )

    logger.info(
        f"Found {len(payment_ids_to_delete)} expired payments to clean up "
        f"across {len(tickets_to_release_by_tier)} tiers."
    )

    with transaction.atomic():
        # Cancel any series pass whose checkout these payments belonged to first
        # (releasing SeriesPass.quantity_sold so the buyer can purchase again),
        # THEN release tier capacity — pass row before tier rows, matching
        # SeriesPassPurchaseService.purchase's lock order to avoid deadlocking
        # against a concurrent purchase on the same pass.
        expire_stranded_held_passes(expired_session_ids)

        # Atomically decrement the quantity_sold for each affected tier.
        for tier_id, count_to_release in tickets_to_release_by_tier.items():
            TicketTier.objects.select_for_update().filter(pk=tier_id).update(
                quantity_sold=F("quantity_sold") - count_to_release
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
