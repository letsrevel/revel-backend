"""Bulk per-event seat overrides (spec §2): hold/kill/release under seat locks."""

import uuid

from django.db import transaction

from events.models import Event, EventSeatOverride, Ticket, VenueSeat
from events.schema.seating import SeatOverridesResponse


@transaction.atomic
def apply_overrides(
    event: Event,
    set_items: list[tuple[uuid.UUID, str, str]],
    release_seat_ids: list[uuid.UUID],
) -> SeatOverridesResponse:
    """Applies/updates/releases overrides. Ticketed seats are rejected per-seat, never the whole batch.

    Lock protocol: locks the affected seat rows (ordered by PK) before checking tickets, so this
    write serializes against any concurrent transaction that locks the same seat rows.

    If a seat id appears in both ``set_items`` and ``release_seat_ids``, the release wins: the
    override is applied then immediately deleted, so the seat ends up with no override row.

    Args:
        event: The event whose seat overrides are being mutated.
        set_items: ``(seat_id, status, reason)`` tuples to upsert (hold/kill).
        release_seat_ids: Seat ids whose override should be deleted.

    Returns:
        A ``SeatOverridesResponse`` with counts and a per-seat ``rejected`` map
        (``"unknown_seat"`` for ids not on this venue, ``"ticketed"`` for seats
        holding a PENDING/ACTIVE ticket on this event).
    """
    all_ids = sorted({sid for sid, _, _ in set_items} | set(release_seat_ids))
    locked_ids = set(
        VenueSeat.objects.filter(id__in=all_ids).order_by("pk").select_for_update().values_list("id", flat=True)
    )
    rejected: dict[uuid.UUID, str] = {sid: "unknown_seat" for sid in all_ids if sid not in locked_ids}

    ticketed = set(
        Ticket.objects.filter(
            event=event,
            seat_id__in=locked_ids,
            status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
        ).values_list("seat_id", flat=True)
    )

    applied = 0
    for seat_id, status, reason in set_items:
        if seat_id in rejected:
            continue
        if seat_id in ticketed:
            rejected[seat_id] = "ticketed"
            continue
        EventSeatOverride.objects.update_or_create(
            event=event, seat_id=seat_id, defaults={"status": status, "reason": reason}
        )
        applied += 1

    release_ok = [sid for sid in release_seat_ids if sid not in rejected]
    released, _ = EventSeatOverride.objects.filter(event=event, seat_id__in=release_ok).delete()
    return SeatOverridesResponse(applied=applied, released=released, rejected=rejected)
