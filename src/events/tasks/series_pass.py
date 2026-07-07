"""Celery tasks for series pass materialization."""

import functools
from uuid import UUID

import structlog
from celery import shared_task
from django.db import transaction
from django.db.models import F

from events.models import HeldSeriesPass, SeriesPass, SeriesPassTierLink, Ticket, TicketTier

logger = structlog.get_logger(__name__)


@shared_task(name="events.materialize_series_pass_holders")
def materialize_series_pass_holders(series_pass_id: str, event_ids: list[str]) -> None:
    """Materialize tickets for all ACTIVE holders of a pass over newly-linked events.

    Free of charge (no Payment rows) — holders paid for the season. Idempotent:
    events already ticketed for a holder are skipped (``materialize_tickets``'s
    existing-events skip plus the DB constraint). Capacity-checked per
    holder/event; skipped pairs are counted and reported.

    Args:
        series_pass_id: The SeriesPass whose holders should be extended.
        event_ids: The newly-linked event ids to materialize tickets for.

    Note:
        Holder ids are snapshotted with ``list()`` rather than ``.iterator()``:
        production runs PgBouncer in transaction-pooling mode, and a server-side
        cursor cannot survive the per-holder commits below (see
        docs/engineering-notes.md, "Server-Side Cursors & Connection Pooling").
    """
    from events.service import series_pass_service
    from notifications.signals.series_pass import send_series_pass_extended

    series_pass = SeriesPass.objects.select_related("event_series__organization").get(pk=UUID(series_pass_id))
    event_pks = [UUID(e) for e in event_ids]
    links = list(
        SeriesPassTierLink.objects.filter(series_pass=series_pass, event_id__in=event_pks).select_related(
            "event", "tier"
        )
    )
    holder_ids = list(
        HeldSeriesPass.objects.filter(series_pass=series_pass, status=HeldSeriesPass.Status.ACTIVE).values_list(
            "id", flat=True
        )
    )
    skipped: list[tuple[UUID, UUID]] = []

    for holder_id in holder_ids:
        with transaction.atomic():
            # of=("self",): lock the pass row only, not the joined user row. The lock
            # makes the ACTIVE re-check authoritative — without it a concurrent
            # cancel_held_pass could commit CANCELLED between this check and the
            # bulk_create below, leaving a cancelled (and refunded) pass holding a
            # live ACTIVE ticket. Lock order pass -> tiers matches cancel_held_pass.
            held_pass = HeldSeriesPass.objects.select_for_update(of=("self",)).select_related("user").get(pk=holder_id)
            if held_pass.status != HeldSeriesPass.Status.ACTIVE:
                # Re-check inside the loop: a concurrent cancellation may have
                # landed between the snapshot above and this holder's turn.
                continue
            locked = {
                tier.pk: tier
                for tier in TicketTier.objects.select_for_update()
                .filter(pk__in=[link.tier_id for link in links])
                .order_by("pk")
            }
            grantable: list[SeriesPassTierLink] = []
            for link in links:
                tier = locked[link.tier_id]
                if tier.total_quantity is not None and tier.quantity_sold >= tier.total_quantity:
                    skipped.append((held_pass.id, link.event_id))
                    continue
                grantable.append(link)
            created = series_pass_service.materialize_tickets(held_pass, grantable, Ticket.TicketStatus.ACTIVE)
            for ticket in created:
                TicketTier.objects.filter(pk=ticket.tier_id).update(quantity_sold=F("quantity_sold") + 1)
            if created:
                # functools.partial binds held_pass.id/event_ids eagerly (not a late-binding
                # closure over the loop variables), matching the engineering-notes guidance
                # for dispatching per-iteration on_commit callbacks from inside a loop.
                transaction.on_commit(
                    functools.partial(send_series_pass_extended, held_pass.id, [c.event_id for c in created])
                )

    logger.info(
        "series_pass_extension_done",
        series_pass_id=series_pass_id,
        holders=len(holder_ids),
        skipped=len(skipped),
    )
