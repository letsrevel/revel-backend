"""Tests for seat hold acquisition/release and guest hold sessions."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeatOverride,
    Organization,
    SeatHold,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)
from events.service.guest_hold_session import issue_guest_hold_token, resolve_guest_session
from events.service.seating import holds as holds_service


def test_guest_token_roundtrip() -> None:
    session_id, token = issue_guest_hold_token()
    assert resolve_guest_session(token) == session_id


def test_guest_token_tamper_rejected() -> None:
    _, token = issue_guest_hold_token()
    assert resolve_guest_session(token[:-2] + "xx") is None
    assert resolve_guest_session(None) is None
    assert resolve_guest_session("") is None


# --- acquire/release/verify_and_consume ------------------------------------

pytestmark = pytest.mark.django_db


@pytest.fixture
def revel_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="seat_holder@example.com", email="seat_holder@example.com")


@pytest.fixture
def other_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="other_holder@example.com", email="other_holder@example.com")


def test_acquire_free_seats(seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser) -> None:
    event, seats = seated_event
    result = holds_service.acquire_seats(event, [seats[0].id, seats[1].id], user=revel_user, guest_session=None)
    assert result.conflicts == []
    assert {h.seat_id for h in result.held} == {seats[0].id, seats[1].id}
    assert result.expires_at is not None


def test_acquire_conflict_is_all_or_nothing(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser, other_user: RevelUser
) -> None:
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=other_user, guest_session=None)
    result = holds_service.acquire_seats(event, [seats[0].id, seats[1].id], user=revel_user, guest_session=None)
    assert result.conflicts == [seats[0].id]
    assert SeatHold.objects.active().filter(seat=seats[1]).count() == 0  # nothing kept


def test_conflict_rolls_back_holds_created_earlier_in_same_request(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser, other_user: RevelUser
) -> None:
    """All-or-nothing: a conflict late in the (seat-id-ordered) loop discards earlier new holds."""
    event, seats = seated_event
    # The upsert loop processes seats in ascending seat-id order; make the foreign
    # hold sit on the LAST seat so every other seat is newly inserted first.
    ordered = sorted(seats[:4], key=lambda s: s.id)
    conflicted = ordered[-1]
    holds_service.acquire_seats(event, [conflicted.id], user=other_user, guest_session=None)
    assert SeatHold.objects.count() == 1

    result = holds_service.acquire_seats(event, [s.id for s in ordered], user=revel_user, guest_session=None)

    assert result.conflicts == [conflicted.id]
    assert result.held == []
    # Only the foreign hold survives: all new holds from this request rolled back.
    assert SeatHold.objects.count() == 1
    assert SeatHold.objects.get().user_id == other_user.id


def test_expired_hold_is_taken_over(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser, other_user: RevelUser
) -> None:
    event, seats = seated_event
    now = timezone.now()
    SeatHold.objects.create(
        event=event,
        seat=seats[0],
        user=other_user,
        acquired_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=10),
    )
    result = holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)
    assert result.conflicts == []
    hold = SeatHold.objects.get(event=event, seat=seats[0])
    assert hold.user_id == revel_user.id
    # Takeover resets the lifetime clock (Postgres now() is transaction-start time,
    # so compare against the stale value rather than a mid-test timezone.now()).
    assert hold.acquired_at > now - timedelta(minutes=1)


def test_live_foreign_hold_is_not_taken_over(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser, other_user: RevelUser
) -> None:
    """The upsert WHERE arm must leave a live hold of another identity untouched."""
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=other_user, guest_session=None)
    before = SeatHold.objects.get(event=event, seat=seats[0])

    result = holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)

    assert result.conflicts == [seats[0].id]
    after = SeatHold.objects.get(event=event, seat=seats[0])
    assert after.user_id == other_user.id
    assert after.expires_at == before.expires_at
    assert after.acquired_at == before.acquired_at


def test_reacquire_refreshes_ttl_but_lifetime_capped(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser
) -> None:
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)
    hold = SeatHold.objects.get(event=event, seat=seats[0])
    SeatHold.objects.filter(pk=hold.pk).update(acquired_at=timezone.now() - timedelta(minutes=29))
    result = holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)
    assert result.conflicts == []
    hold.refresh_from_db()
    # Own-refresh preserves acquired_at, and expires_at never exceeds the absolute lifetime.
    assert hold.acquired_at <= timezone.now() - timedelta(minutes=28)
    assert hold.expires_at <= hold.acquired_at + holds_service.HOLD_MAX_LIFETIME


def test_guest_refresh_extends_own_hold(seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser) -> None:
    """A guest identity can refresh its own live hold (user_id NULL arm of the upsert)."""
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=None, guest_session="gs-refresh")
    first = SeatHold.objects.get(event=event, seat=seats[0])
    SeatHold.objects.filter(pk=first.pk).update(expires_at=first.expires_at - timedelta(minutes=5))

    result = holds_service.acquire_seats(event, [seats[0].id], user=None, guest_session="gs-refresh")

    assert result.conflicts == []
    refreshed = SeatHold.objects.get(event=event, seat=seats[0])
    assert refreshed.pk == first.pk  # refreshed in place, not recreated
    assert refreshed.guest_session == "gs-refresh"
    assert refreshed.user_id is None
    assert refreshed.expires_at > first.expires_at - timedelta(minutes=5)


def test_cap_enforced(seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser) -> None:
    event, seats = seated_event
    event.max_tickets_per_user = 2
    event.save(update_fields=["max_tickets_per_user"])
    result = holds_service.acquire_seats(event, [s.id for s in seats[:3]], user=revel_user, guest_session=None)
    assert result.conflicts  # over-cap rejected as a whole
    assert SeatHold.objects.active().filter(event=event).count() == 0


def test_cap_counts_existing_holds_but_not_reacquired_seats(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser
) -> None:
    event, seats = seated_event
    event.max_tickets_per_user = 2
    event.save(update_fields=["max_tickets_per_user"])
    holds_service.acquire_seats(event, [seats[0].id, seats[1].id], user=revel_user, guest_session=None)
    # Re-acquiring already-held seats stays within the cap...
    result = holds_service.acquire_seats(event, [seats[0].id, seats[1].id], user=revel_user, guest_session=None)
    assert result.conflicts == []
    # ...but one more seat exceeds it.
    result = holds_service.acquire_seats(event, [seats[2].id], user=revel_user, guest_session=None)
    assert result.conflicts == [seats[2].id]


def test_ticketed_seat_conflicts(
    seated_event: tuple[Event, list[VenueSeat]],
    revel_user: RevelUser,
    other_user: RevelUser,
    event_ticket_tier: TicketTier,
) -> None:
    event, seats = seated_event
    Ticket.objects.create(
        event=event,
        tier=event_ticket_tier,
        user=other_user,
        seat=seats[0],
        sector=seats[0].sector,
        guest_name=other_user.get_display_name(),
    )
    result = holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)
    assert result.conflicts == [seats[0].id]


def test_checked_in_seat_conflicts(
    seated_event: tuple[Event, list[VenueSeat]],
    revel_user: RevelUser,
    other_user: RevelUser,
    event_ticket_tier: TicketTier,
) -> None:
    """A CHECKED_IN ticket occupies the seat (unique_ticket_event_seat covers all
    non-cancelled statuses) — the seat must not be holdable."""
    event, seats = seated_event
    Ticket.objects.create(
        event=event,
        tier=event_ticket_tier,
        user=other_user,
        seat=seats[0],
        sector=seats[0].sector,
        status=Ticket.TicketStatus.CHECKED_IN,
        checked_in_at=timezone.now(),
        guest_name=other_user.get_display_name(),
    )
    result = holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)
    assert result.conflicts == [seats[0].id]


def test_cancelled_ticket_frees_seat(
    seated_event: tuple[Event, list[VenueSeat]],
    revel_user: RevelUser,
    other_user: RevelUser,
    event_ticket_tier: TicketTier,
) -> None:
    event, seats = seated_event
    Ticket.objects.create(
        event=event,
        tier=event_ticket_tier,
        user=other_user,
        seat=seats[0],
        sector=seats[0].sector,
        status=Ticket.TicketStatus.CANCELLED,
        guest_name=other_user.get_display_name(),
    )
    result = holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)
    assert result.conflicts == []


def test_unholdable_seats_conflict(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser, organization: Organization
) -> None:
    """Inactive, standing-sector, overridden, and foreign-venue seats are all rejected."""
    event, seats = seated_event
    inactive = seats[0]
    inactive.is_active = False
    inactive.save(update_fields=["is_active"])

    assert event.venue is not None
    standing_sector = VenueSector.objects.create(venue=event.venue, name="Pit", kind=VenueSector.Kind.STANDING)
    standing_seat = VenueSeat.objects.create(sector=standing_sector, label="P1")

    overridden = seats[1]
    EventSeatOverride.objects.create(event=event, seat=overridden, status=EventSeatOverride.OverrideStatus.KILLED)

    other_venue = Venue.objects.create(organization=organization, name="Elsewhere")
    other_sector = VenueSector.objects.create(venue=other_venue, name="Stalls")
    foreign_seat = VenueSeat.objects.create(sector=other_sector, label="X1")

    for seat in (inactive, standing_seat, overridden, foreign_seat):
        result = holds_service.acquire_seats(event, [seat.id], user=revel_user, guest_session=None)
        assert result.conflicts == [seat.id]
    assert SeatHold.objects.count() == 0


def test_event_without_venue_rejects_all_seats(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser
) -> None:
    event, seats = seated_event
    event.venue = None
    event.save(update_fields=["venue"])
    result = holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)
    assert result.conflicts == [seats[0].id]


def test_guest_and_user_are_distinct_identities(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser
) -> None:
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=None, guest_session="gs-1")
    result = holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)
    assert result.conflicts == [seats[0].id]


def test_anonymous_without_guest_session_raises(
    seated_event: tuple[Event, list[VenueSeat]],
) -> None:
    event, seats = seated_event
    with pytest.raises(ValueError):
        holds_service.acquire_seats(event, [seats[0].id], user=None, guest_session=None)


def test_release_seats(seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser) -> None:
    event, seats = seated_event
    holds_service.acquire_seats(event, [s.id for s in seats[:3]], user=revel_user, guest_session=None)
    assert holds_service.release_seats(event, [seats[0].id], user=revel_user, guest_session=None) == 1
    assert holds_service.release_seats(event, None, user=revel_user, guest_session=None) == 2
    assert SeatHold.objects.count() == 0


def test_release_only_touches_own_holds(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser, other_user: RevelUser
) -> None:
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=other_user, guest_session=None)
    assert holds_service.release_seats(event, None, user=revel_user, guest_session=None) == 0
    assert SeatHold.objects.count() == 1


def test_verify_and_consume(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser, other_user: RevelUser
) -> None:
    event, seats = seated_event
    holds_service.acquire_seats(event, [seats[0].id], user=other_user, guest_session=None)
    with pytest.raises(holds_service.SeatHoldConflictError) as exc_info:
        holds_service.verify_and_consume_holds(event, [seats[0].id], user=revel_user, guest_session=None)
    assert exc_info.value.seat_ids == [seats[0].id]
    holds_service.acquire_seats(event, [seats[1].id], user=revel_user, guest_session=None)
    holds_service.verify_and_consume_holds(event, [seats[1].id], user=revel_user, guest_session=None)
    assert not SeatHold.objects.filter(seat=seats[1]).exists()


def test_verify_ignores_expired_foreign_hold(
    seated_event: tuple[Event, list[VenueSeat]], revel_user: RevelUser, other_user: RevelUser
) -> None:
    event, seats = seated_event
    now = timezone.now()
    SeatHold.objects.create(
        event=event,
        seat=seats[0],
        user=other_user,
        acquired_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=1),
    )
    holds_service.verify_and_consume_holds(event, [seats[0].id], user=revel_user, guest_session=None)
