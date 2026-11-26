"""Celery tasks for series pass materialization."""

import functools
from decimal import Decimal
from uuid import UUID

import structlog
from celery import shared_task
from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from events.models import Event, HeldSeriesPass, SeriesPass, SeriesPassTierLink, Ticket, TicketTier

logger = structlog.get_logger(__name__)


@shared_task(name="events.materialize_series_pass_holders")
def materialize_series_pass_holders(series_pass_id: str, event_ids: list[str]) -> None:
    """Materialize tickets for all ACTIVE holders of a pass over newly-linked events.

    Free of charge (no Payment rows) — holders paid for the season. Idempotent:
    events already ticketed for a holder are skipped (``materialize_tickets``'s
    existing-events skip plus the DB constraint). Capacity-checked per
    holder/event (tier and event level); holders now blacklisted for the org are skipped
    entirely; skipped pairs are counted and reported.

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

    series_pass = SeriesPass.objects.select_related("event_series__organization").get(pk=UUID(series_pass_id))
    event_pks = [UUID(e) for e in event_ids]
    # An event cancelled before this task runs gets no ticket (#1494).
    links = list(
        SeriesPassTierLink.objects.filter(series_pass=series_pass, event_id__in=event_pks)
        .exclude(event__status=Event.EventStatus.CANCELLED)
        .select_related("event", "tier")
    )
    holder_ids = list(
        HeldSeriesPass.objects.filter(
            series_pass=series_pass, status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE
        ).values_list("id", flat=True)
    )
    skipped: list[tuple[UUID, UUID]] = []
    created_event_ids: set[UUID] = set()

    try:
        for holder_id in holder_ids:
            created_event_ids.update(ticket.event_id for ticket in _extend_holder(holder_id, links, skipped))
    finally:
        # bulk_create skips post_save; rebuild once per event (not per holder). In a finally so
        # holders that already committed still get their rebuild if a later holder raises: a
        # re-run skips already-ticketed holders, so it would never re-dispatch for them.
        series_pass_service.rebuild_attendee_visibility(created_event_ids)

    logger.info(
        "series_pass_extension_done",
        series_pass_id=series_pass_id,
        holders=len(holder_ids),
        skipped=len(skipped),
    )


@shared_task(name="events.reconcile_series_pass_tickets")
def reconcile_series_pass_tickets() -> int:
    """Give ACTIVE holders the covered-event tickets that a failed dispatch never issued (#1536).

    ``materialize_series_pass_holders`` is enqueued after commit by one ``.delay()``. If the enqueue
    fails, nothing runs it again. This sweep finds each ACTIVE held pass with no ticket row at all for
    a covered event that is not cancelled and has not started, and runs the extension for it, with the
    same capacity and blacklist checks.

    A ticket row of any status counts: an organizer-cancelled ticket is never issued again. A holder
    that was skipped (full tier or event, blacklisted) is tried again on each run, so a freed seat gives
    that holder the ticket.

    Returns:
        The number of tickets created.
    """
    from events.service import series_pass_service

    now = timezone.now()
    holder_without_ticket = HeldSeriesPass.objects.filter(
        series_pass_id=OuterRef("series_pass_id"), status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE
    ).exclude(Exists(Ticket.objects.filter(held_pass=OuterRef("pk"), event_id=OuterRef(OuterRef("event_id")))))
    links = list(
        SeriesPassTierLink.objects.filter(event__start__gte=now)
        .exclude(event__status=Event.EventStatus.CANCELLED)
        .filter(Exists(holder_without_ticket))
        .select_related("event__organization")
    )
    # ponytail: one holder query per link with a gap, and a holder that stays skipped (blacklisted, or a tier
    # that stays full) is tried again, and logged, on every run until the event starts. Gaps are rare (a failed
    # enqueue or a skipped holder). Upgrade path: one pair query, and leave blacklisted holders out of it.
    links_by_holder: dict[UUID, list[SeriesPassTierLink]] = {}
    for link in links:
        holder_ids = (
            HeldSeriesPass.objects.filter(
                series_pass_id=link.series_pass_id, status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE
            )
            .exclude(Exists(Ticket.objects.filter(held_pass=OuterRef("pk"), event_id=link.event_id)))
            .values_list("id", flat=True)
        )
        for holder_id in holder_ids:
            links_by_holder.setdefault(holder_id, []).append(link)

    skipped: list[tuple[UUID, UUID]] = []
    created_event_ids: set[UUID] = set()
    created = 0
    try:
        for holder_id, holder_links in links_by_holder.items():
            tickets = _extend_holder(holder_id, holder_links, skipped)
            created += len(tickets)
            created_event_ids.update(ticket.event_id for ticket in tickets)
    finally:
        # bulk_create skips post_save; one rebuild per event, also when a later holder raises.
        series_pass_service.rebuild_attendee_visibility(created_event_ids)

    if links_by_holder:
        logger.info("series_pass_reconcile_done", holders=len(links_by_holder), created=created, skipped=len(skipped))
    return created


def _lock_tiers_and_drop_full(
    held_pass_id: UUID, links: list[SeriesPassTierLink], skipped: list[tuple[UUID, UUID]]
) -> list[SeriesPassTierLink]:
    """Lock the mapped tiers pk-ordered; return the links whose tier has room, reporting full ones as skipped.

    A link whose tier is gone (its covered event was deleted after the links snapshot) is dropped.
    """
    locked = {
        tier.pk: tier
        for tier in TicketTier.objects.select_for_update()
        .filter(pk__in=[link.tier_id for link in links])
        .order_by("pk")
    }
    grantable: list[SeriesPassTierLink] = []
    for link in links:
        tier = locked.get(link.tier_id)
        if tier is None:
            logger.info(
                "series_pass_extension_skipped_deleted_event",
                held_pass_id=str(held_pass_id),
                event_id=str(link.event_id),
            )
            continue
        if tier.total_quantity is not None and tier.quantity_sold >= tier.total_quantity:
            skipped.append((held_pass_id, link.event_id))
            continue
        grantable.append(link)
    return grantable


def _extend_holder(holder_id: UUID, links: list[SeriesPassTierLink], skipped: list[tuple[UUID, UUID]]) -> list[Ticket]:
    """Materialize one ACTIVE holder's tickets in its own transaction; returns the created tickets."""
    from events.service import series_pass_event_checks, series_pass_service
    from notifications.signals.series_pass import send_series_pass_extended

    with transaction.atomic():
        # of=("self",): lock the pass row only, not the joined user row. The lock
        # makes the ACTIVE re-check authoritative — without it a concurrent
        # cancel_held_pass could commit CANCELLED between this check and the
        # bulk_create below, leaving a cancelled (and refunded) pass holding a
        # live ACTIVE ticket. Lock order pass -> tiers matches cancel_held_pass.
        held_pass = HeldSeriesPass.objects.select_for_update(of=("self",)).select_related("user").get(pk=holder_id)
        if held_pass.status != HeldSeriesPass.HeldSeriesPassStatus.ACTIVE:
            # Re-check per holder: a concurrent cancellation may have
            # landed between the holder snapshot and this holder's turn.
            return []
        # Events the holder already has a ticket for, of any status, are not extension work: drop them
        # before any capacity check so a full event they already hold is not reported as skipped. A CANCELLED
        # ticket counts too: the organizer cancelled it, and no extension, un-cancel or sweep issues it again.
        # Read under the pass lock, so a ticket issued and cancelled after the sweep's snapshot counts (#1536).
        ticketed_event_ids = set(Ticket.objects.filter(held_pass=held_pass).values_list("event_id", flat=True))
        pending_links = [link for link in links if link.event_id not in ticketed_event_ids]
        if not pending_links:
            return []
        # The holder's current blacklist status (the purchase's rule, owners/staff exempt). Org-wide and
        # lock-free, so it runs before the contended tier locks. ponytail: one EligibilityService per holder.
        if series_pass_event_checks.is_blocked_by_blacklist(held_pass.user, pending_links[0].event):
            logger.info("series_pass_extension_skipped_blacklisted_holder", held_pass_id=str(held_pass.id))
            skipped.extend((held_pass.id, link.event_id) for link in pending_links)
            return []
        grantable = _lock_tiers_and_drop_full(held_pass.id, pending_links, skipped)
        # Free of charge — holders paid for the season, so extended tickets never
        # inherit the mapped tier's price (keeps revenue/VAT reports honest, #644).
        # ponytail: holders never answered a questionnaire attached to the newly linked event; tickets are issued
        # anyway. Upgrade path: gate extension tickets on the questionnaire, or notify holders to answer it.
        # Event-level capacity (events locked after the tiers, pk-ordered), skipped and reported like full tiers.
        locked_events = series_pass_event_checks.lock_events_and_find_full(
            [link.event for link in grantable], held_pass.user
        )
        # Read under the lock, with a new ``now``: an event cancelled or started after the links snapshot
        # gets no ticket, and no skip report (#1496).
        now = timezone.now()
        deleted = {link.event_id for link in grantable if link.event_id not in locked_events.status}
        gone = locked_events.cancelled | {eid for eid, start in locked_events.start.items() if start < now} | deleted
        for link in grantable:
            if link.event_id in gone:
                logger.info(
                    "series_pass_extension_skipped_deleted_event"
                    if link.event_id in deleted
                    else "series_pass_extension_skipped_cancelled_or_started_event",
                    held_pass_id=str(held_pass.id),
                    event_id=str(link.event_id),
                )
            elif link.event_id in locked_events.full:
                skipped.append((held_pass.id, link.event_id))
        excluded = gone | locked_events.full
        grantable = [link for link in grantable if link.event_id not in excluded]
        created = series_pass_service.materialize_tickets(
            held_pass, grantable, Ticket.TicketStatus.ACTIVE, price_paid=Decimal("0.00")
        )
        if created:
            # One ticket per tier by construction (each covered event has its own tier
            # link) — a single UPDATE increments them all instead of one query per ticket.
            TicketTier.objects.filter(pk__in=[ticket.tier_id for ticket in created]).update(
                quantity_sold=F("quantity_sold") + 1
            )
            # functools.partial binds held_pass.id/event_ids eagerly (not a late-binding
            # closure), matching the engineering-notes guidance for dispatching
            # per-iteration on_commit callbacks from inside a loop.
            transaction.on_commit(
                functools.partial(send_series_pass_extended, held_pass.id, [c.event_id for c in created])
            )
    return created
