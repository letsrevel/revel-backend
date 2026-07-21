"""DB-backed best-available pick: candidate loading, category filtering, optimistic hold + retry."""

import typing as t

import pytest

from accounts.models import RevelUser
from events.models import Event, PriceCategory, TicketTier, VenueSeat, VenueSector
from events.service.seating import holds as holds_service
from events.service.seating import pick
from events.service.seating.holds import HoldResult
from events.service.seating.holds import acquire_seats as real_acquire

pytestmark = pytest.mark.django_db


def _category(event: Event, name: str = "Std", color: str = "#00aa00") -> PriceCategory:
    venue = event.venue
    assert venue is not None
    return PriceCategory.objects.create(venue=venue, name=name, color=color)


def _paint(seats: list[VenueSeat], cat: PriceCategory) -> None:
    for s in seats:
        s.default_price_category = cat
        s.save(update_fields=["default_price_category"])


def _tier(event: Event, cat: PriceCategory) -> TicketTier:
    """A best-available tier whose single sellable zone is ``cat`` (v3: zone = map key)."""
    sector = VenueSector.objects.filter(venue=event.venue).first()
    assert sector is not None
    return TicketTier.objects.create(
        event=event,
        name="Std",
        sector=sector,
        category_prices={str(cat.id): "0"},
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
    )


def test_hold_best_available_end_to_end(seated_event: tuple[Event, list[VenueSeat]], member_user: RevelUser) -> None:
    event, seats = seated_event
    cat = _category(event)
    _paint(seats, cat)
    tier = _tier(event, cat)

    result = pick.hold_best_available(event, tier, 2, user=member_user, guest_session=None, price_category_id=cat.id)

    assert result.conflicts == []
    assert len(result.held) == 2
    held = sorted(h.seat.adjacency_index for h in result.held)
    assert held[1] - held[0] == 1  # adjacent


def test_hold_best_available_insufficient(seated_event: tuple[Event, list[VenueSeat]], member_user: RevelUser) -> None:
    event, seats = seated_event
    cat = _category(event)
    tier = _tier(event, cat)  # no seats painted with the category

    result = pick.hold_best_available(event, tier, 2, user=member_user, guest_session=None, price_category_id=cat.id)

    assert result.held == []
    assert result.conflicts == []


def test_hold_best_available_only_picks_own_category(
    seated_event: tuple[Event, list[VenueSeat]], member_user: RevelUser
) -> None:
    event, seats = seated_event  # 6 seats, adjacency 0..5
    cat_a = _category(event, "A")
    cat_b = _category(event, "B", "#0000aa")
    _paint(seats[:2], cat_a)  # only two seats in category A -> exactly the block
    _paint(seats[2:], cat_b)
    tier = _tier(event, cat_a)

    result = pick.hold_best_available(event, tier, 2, user=member_user, guest_session=None, price_category_id=cat_a.id)

    held_ids = {h.seat_id for h in result.held}
    assert held_ids == {seats[0].id, seats[1].id}


def test_hold_best_available_accessible_required(
    seated_event: tuple[Event, list[VenueSeat]], member_user: RevelUser
) -> None:
    event, seats = seated_event
    cat = _category(event)
    _paint(seats, cat)
    for s in seats[:2]:  # mark the first two accessible
        s.is_accessible = True
        s.save(update_fields=["is_accessible"])
    tier = _tier(event, cat)

    result = pick.hold_best_available(
        event, tier, 2, user=member_user, guest_session=None, accessible_required=True, price_category_id=cat.id
    )

    held_ids = {h.seat_id for h in result.held}
    assert held_ids == {seats[0].id, seats[1].id}


def test_hold_best_available_avoids_already_held_seats(
    seated_event: tuple[Event, list[VenueSeat]], member_user: RevelUser, public_user: RevelUser
) -> None:
    event, seats = seated_event  # adjacency 0..5
    cat = _category(event)
    _paint(seats, cat)
    tier = _tier(event, cat)
    # Someone else holds the centre seat that would otherwise be the best pick.
    holds_service.acquire_seats(event, [seats[2].id], user=public_user, guest_session=None)

    result = pick.hold_best_available(event, tier, 2, user=member_user, guest_session=None, price_category_id=cat.id)

    held_ids = {h.seat_id for h in result.held}
    assert seats[2].id not in held_ids
    assert len(result.held) == 2


def test_hold_best_available_retries_on_conflict(
    seated_event: tuple[Event, list[VenueSeat]],
    member_user: RevelUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TOCTOU loss on the first acquire triggers a re-load/re-pick that excludes the lost seats."""
    event, seats = seated_event
    cat = _category(event)
    _paint(seats, cat)
    tier = _tier(event, cat)

    calls: list[list[t.Any]] = []

    def fake_acquire(ev: Event, seat_ids: list[t.Any], *, user: t.Any, guest_session: t.Any) -> HoldResult:
        calls.append(list(seat_ids))
        if len(calls) == 1:  # simulate the picked block being taken from under us
            return HoldResult(held=[], conflicts=list(seat_ids), expires_at=None)
        return real_acquire(ev, seat_ids, user=user, guest_session=guest_session)

    monkeypatch.setattr(pick, "acquire_seats", fake_acquire)

    result = pick.hold_best_available(event, tier, 2, user=member_user, guest_session=None, price_category_id=cat.id)

    assert len(calls) == 2
    assert set(calls[1]).isdisjoint(set(calls[0]))  # retry excluded the lost seats
    assert result.conflicts == []
    assert len(result.held) == 2


def test_load_candidates_stable_order(seated_event: tuple[Event, list[VenueSeat]]) -> None:
    event, seats = seated_event
    cat = _category(event)
    _paint(seats, cat)
    tier = _tier(event, cat)

    ids = [c.id for c in pick.load_candidates(event, tier, set(), zone_id=cat.id)]

    assert ids == sorted(ids)  # deterministic PK order for the seeded tiebreak
