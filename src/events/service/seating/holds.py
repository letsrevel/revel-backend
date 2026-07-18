"""Seat hold acquisition/release with takeover upsert (spec §2).

A time-predicate partial unique index is impossible in Postgres (now() is not
IMMUTABLE), so SeatHold has an unconditional unique (event, seat) and expired
rows are claimed IN PLACE via INSERT ... ON CONFLICT DO UPDATE ... WHERE expired.
Row locks are taken in seat-PK order (sorted seat_ids) per the global protocol.
"""

import dataclasses
import typing as t
import uuid
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.db.models import DateTimeField, ExpressionWrapper, F, Q
from django.db.models.functions import Least
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeatOverride, SeatHold, Ticket, VenueSeat, VenueSector

HOLD_TTL = timedelta(minutes=10)
HOLD_MAX_LIFETIME = timedelta(minutes=30)
DEFAULT_MAX_HELD_SEATS = 10

_UPSERT_SQL = """
INSERT INTO events_seathold
    (id, created_at, updated_at, event_id, seat_id, user_id, guest_session, acquired_at, expires_at)
VALUES (%(id)s, now(), now(), %(event_id)s, %(seat_id)s, %(user_id)s, %(guest_session)s, now(), %(expires_at)s)
ON CONFLICT (event_id, seat_id) DO UPDATE
SET user_id = EXCLUDED.user_id,
    guest_session = EXCLUDED.guest_session,
    acquired_at = CASE
        WHEN events_seathold.expires_at <= now() THEN EXCLUDED.acquired_at
        ELSE events_seathold.acquired_at
    END,
    expires_at = EXCLUDED.expires_at,
    updated_at = now()
WHERE events_seathold.expires_at <= now()
   OR (events_seathold.user_id IS NOT DISTINCT FROM EXCLUDED.user_id
       AND events_seathold.guest_session = EXCLUDED.guest_session)
RETURNING id
"""
# The second WHERE arm lets an identity re-acquire its own live hold (TTL refresh);
# the CASE resets acquired_at ONLY on takeover of an expired row — own-refresh keeps
# it, and the post-upsert clamp bounds expires_at to acquired_at + HOLD_MAX_LIFETIME.


class SeatHoldConflictError(Exception):
    """One or more seats are live-held by another identity."""

    def __init__(self, seat_ids: list[uuid.UUID]) -> None:
        """Store the conflicting seat ids for the caller to report."""
        self.seat_ids = seat_ids
        super().__init__(f"{len(seat_ids)} seat(s) unavailable")


@dataclasses.dataclass
class HoldResult:
    held: list[SeatHold]
    conflicts: list[uuid.UUID]
    expires_at: datetime | None
    # Why the conflicts exist: "capacity" (caller holds too many seats) or
    # "unavailable" (seats invalid/blocked/sold/held by someone else). None on success.
    conflict_reason: str | None = None


def _identity_params(user: RevelUser | None, guest_session: str | None) -> dict[str, t.Any]:
    if user is not None and user.is_authenticated:
        return {"user_id": str(user.id), "guest_session": ""}
    if not guest_session:
        raise ValueError("Anonymous hold requires a guest session.")
    return {"user_id": None, "guest_session": guest_session}


def _validate_holdable(event: Event, seat_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Return the subset of seat_ids that are NOT holdable (bad/blocked/sold)."""
    if event.venue_id is None:
        return list(seat_ids)
    valid_ids = set(
        VenueSeat.objects.filter(
            id__in=seat_ids,
            is_active=True,
            sector__kind=VenueSector.Kind.SEATED,
            sector__venue_id=event.venue_id,
        ).values_list("id", flat=True)
    )
    bad = [sid for sid in seat_ids if sid not in valid_ids]
    blocked = set(EventSeatOverride.objects.filter(event=event, seat_id__in=seat_ids).values_list("seat_id", flat=True))
    # Non-cancelled = occupied, matching the unique_ticket_event_seat constraint.
    sold = set(
        Ticket.objects.filter(event=event, seat_id__in=seat_ids)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .values_list("seat_id", flat=True)
    )
    return bad + [sid for sid in seat_ids if sid in blocked or sid in sold]


def _clamp_lifetime(owner_q: Q, event: Event, seat_ids: list[uuid.UUID]) -> None:
    """Bound refreshed TTLs to the absolute lifetime (acquired_at + HOLD_MAX_LIFETIME)."""
    limit = ExpressionWrapper(F("acquired_at") + HOLD_MAX_LIFETIME, output_field=DateTimeField())
    SeatHold.objects.filter(owner_q, event=event, seat_id__in=seat_ids).update(expires_at=Least(F("expires_at"), limit))


def _current_result(
    event: Event,
    user: RevelUser | None,
    guest_session: str | None,
    *,
    conflicts: list[uuid.UUID],
    conflict_reason: str | None = None,
) -> HoldResult:
    owner_q = SeatHold.owner_q(user, guest_session)
    held = list(SeatHold.objects.active().filter(owner_q, event=event).select_related("seat"))
    expires = min((h.expires_at for h in held), default=None)
    return HoldResult(held=held, conflicts=conflicts, expires_at=expires, conflict_reason=conflict_reason)


def acquire_seats(
    event: Event,
    seat_ids: list[uuid.UUID],
    *,
    user: RevelUser | None,
    guest_session: str | None,
) -> HoldResult:
    """All-or-nothing acquisition. Conflicts roll back every new hold in the request."""
    identity = _identity_params(user, guest_session)
    owner_q = SeatHold.owner_q(user, guest_session)
    ordered = sorted(set(seat_ids))  # lock order: seat PK ascending

    cap = event.max_tickets_per_user or DEFAULT_MAX_HELD_SEATS
    already_held = set(SeatHold.objects.active().filter(owner_q, event=event).values_list("seat_id", flat=True))
    if len(already_held | set(ordered)) > cap:
        return _current_result(event, user, guest_session, conflicts=list(ordered), conflict_reason="capacity")

    conflicts = _validate_holdable(event, ordered)
    if conflicts:
        return _current_result(event, user, guest_session, conflicts=conflicts, conflict_reason="unavailable")

    try:
        with transaction.atomic():
            _upsert_holds(event, ordered, identity)
            _clamp_lifetime(owner_q, event, ordered)
    except SeatHoldConflictError as exc:
        return _current_result(event, user, guest_session, conflicts=exc.seat_ids, conflict_reason="unavailable")

    return _current_result(event, user, guest_session, conflicts=[])


def _upsert_holds(event: Event, ordered: list[uuid.UUID], identity: dict[str, t.Any]) -> None:
    """Takeover-upsert each seat in PK order; raise on the first live foreign hold."""
    expires_at = timezone.now() + HOLD_TTL
    with connection.cursor() as cursor:
        for seat_id in ordered:
            cursor.execute(
                _UPSERT_SQL,
                {
                    "id": str(uuid.uuid4()),
                    "event_id": str(event.id),
                    "seat_id": str(seat_id),
                    "expires_at": expires_at,
                    **identity,
                },
            )
            if cursor.rowcount == 0:
                raise SeatHoldConflictError([seat_id])


def release_seats(
    event: Event, seat_ids: list[uuid.UUID] | None, *, user: RevelUser | None, guest_session: str | None
) -> int:
    """Delete this identity's holds on the event (all of them when seat_ids is None)."""
    qs = SeatHold.objects.filter(SeatHold.owner_q(user, guest_session), event=event)
    if seat_ids is not None:
        qs = qs.filter(seat_id__in=seat_ids)
    deleted, _ = qs.delete()
    return deleted


def verify_and_consume_holds(
    event: Event, seat_ids: list[uuid.UUID], *, user: RevelUser | None, guest_session: str | None
) -> None:
    """Purchase-time check: no LIVE foreign hold on any requested seat; own holds are consumed.

    Called inside the purchase transaction AFTER seats are locked (Task 11).

    Raises:
        SeatHoldConflictError: if any requested seat has a live hold owned by another identity.
    """
    owner_q = SeatHold.owner_q(user, guest_session)
    foreign = list(
        SeatHold.objects.active()
        .filter(event=event, seat_id__in=seat_ids)
        .exclude(owner_q)
        .values_list("seat_id", flat=True)
    )
    if foreign:
        raise SeatHoldConflictError(foreign)
    SeatHold.objects.filter(owner_q, event=event, seat_id__in=seat_ids).delete()
