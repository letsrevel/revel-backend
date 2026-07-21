"""Holds ↔ purchase interaction (spec §2): foreign holds block, own holds consume.

Also covers EventSeatOverride enforcement on the direct purchase paths and the
BEST_AVAILABLE seat-assignment mode (Task 11).
"""

import typing as t

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeatOverride,
    PriceCategory,
    SeatHold,
    Ticket,
    TicketTier,
    VenueSeat,
)
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.seating import holds as holds_service

pytestmark = pytest.mark.django_db

SeatedEvent = tuple[Event, list[VenueSeat]]


@pytest.fixture
def revel_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="buyer@example.com", email="buyer@example.com")


@pytest.fixture
def other_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="rival@example.com", email="rival@example.com")


def _user_choice_tier(event: Event, sector_seat: VenueSeat) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="UC",
        sector=sector_seat.sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        payment_method=TicketTier.PaymentMethod.FREE,
    )


def _best_available_tier(event: Event, seats: list[VenueSeat]) -> TicketTier:
    category = PriceCategory.objects.create(venue=seats[0].sector.venue, name="Std", color="#00aa00")
    VenueSeat.objects.filter(id__in=[s.id for s in seats]).update(default_price_category=category)
    return TicketTier.objects.create(
        event=event,
        name="BA",
        sector=seats[0].sector,
        category_prices={str(category.id): "0"},
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        payment_method=TicketTier.PaymentMethod.FREE,
    )


def _item(seat: VenueSeat | None = None) -> TicketPurchaseItem:
    return TicketPurchaseItem(guest_name="Guest", seat_id=seat.id if seat else None)


# --- Holds ↔ USER_CHOICE purchase -------------------------------------------


def test_purchase_rejected_when_seat_held_by_other(
    seated_event: SeatedEvent, revel_user: RevelUser, other_user: RevelUser
) -> None:
    event, seats = seated_event
    tier = _user_choice_tier(event, seats[0])
    holds_service.acquire_seats(event, [seats[0].id], user=other_user, guest_session=None)
    service = BatchTicketService(event, tier, revel_user)
    with pytest.raises(HttpError) as exc:
        service.create_batch(items=[_item(seats[0])])
    assert exc.value.status_code == 409
    assert not Ticket.objects.filter(event=event).exists()


def test_purchase_consumes_own_hold(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    event, seats = seated_event
    tier = _user_choice_tier(event, seats[0])
    holds_service.acquire_seats(event, [seats[0].id], user=revel_user, guest_session=None)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(seats[0])])
    assert Ticket.objects.filter(event=event, seat=seats[0]).exists()
    assert not SeatHold.objects.filter(event=event, seat=seats[0]).exists()


def test_purchase_without_hold_still_works(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    """Holds are advisory — a direct USER_CHOICE purchase of an unheld free seat succeeds."""
    event, seats = seated_event
    tier = _user_choice_tier(event, seats[1])
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(seats[1])])
    assert Ticket.objects.filter(event=event, seat=seats[1]).exists()


def test_guest_session_purchase_consumes_guest_hold(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    """A guest checkout consumes the browser's guest-session holds, not the guest user's."""
    event, seats = seated_event
    tier = _user_choice_tier(event, seats[0])
    holds_service.acquire_seats(event, [seats[0].id], user=None, guest_session="guest-session-abc")
    service = BatchTicketService(event, tier, revel_user, guest_session="guest-session-abc")
    service.create_batch(items=[_item(seats[0])])
    assert Ticket.objects.filter(event=event, seat=seats[0]).exists()
    assert not SeatHold.objects.filter(event=event, seat=seats[0]).exists()


def test_guest_session_purchase_rejected_when_user_holds(
    seated_event: SeatedEvent, revel_user: RevelUser, other_user: RevelUser
) -> None:
    event, seats = seated_event
    tier = _user_choice_tier(event, seats[0])
    holds_service.acquire_seats(event, [seats[0].id], user=other_user, guest_session=None)
    service = BatchTicketService(event, tier, revel_user, guest_session="guest-session-abc")
    with pytest.raises(HttpError) as exc:
        service.create_batch(items=[_item(seats[0])])
    assert exc.value.status_code == 409


# --- Seat overrides on direct purchase paths --------------------------------


def test_user_choice_rejects_killed_seat(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    event, seats = seated_event
    tier = _user_choice_tier(event, seats[2])
    EventSeatOverride.objects.create(event=event, seat=seats[2], status=EventSeatOverride.OverrideStatus.KILLED)
    with pytest.raises(HttpError) as exc:
        BatchTicketService(event, tier, revel_user).create_batch(items=[_item(seats[2])])
    assert exc.value.status_code == 400
    assert not Ticket.objects.filter(event=event).exists()


def test_user_choice_rejects_held_override_seat(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    event, seats = seated_event
    tier = _user_choice_tier(event, seats[2])
    EventSeatOverride.objects.create(event=event, seat=seats[2], status=EventSeatOverride.OverrideStatus.HELD)
    with pytest.raises(HttpError) as exc:
        BatchTicketService(event, tier, revel_user).create_batch(items=[_item(seats[2])])
    assert exc.value.status_code == 400


def test_user_choice_rejects_checked_in_seat(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    """A CHECKED_IN ticket occupies its seat (unique_ticket_event_seat covers all
    non-cancelled statuses) — a USER_CHOICE purchase of that seat must be rejected,
    not die on the DB constraint."""
    event, seats = seated_event
    tier = _user_choice_tier(event, seats[0])
    Ticket.objects.create(
        event=event,
        tier=tier,
        user=revel_user,
        status=Ticket.TicketStatus.CHECKED_IN,
        guest_name="Already In",
        seat=seats[0],
        sector=seats[0].sector,
        venue=seats[0].sector.venue,
    )
    other = RevelUser.objects.create_user(username="late@example.com", email="late@example.com", password="pass")
    with pytest.raises(HttpError) as exc:
        BatchTicketService(event, tier, other).create_batch(items=[_item(seats[0])])
    assert exc.value.status_code == 400
    assert Ticket.objects.filter(event=event).count() == 1


# --- BEST_AVAILABLE mode ----------------------------------------------------


def test_best_available_mode_assigns_adjacent(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    bought = sorted(t.seat.adjacency_index for t in Ticket.objects.filter(event=event).select_related("seat") if t.seat)
    assert len(bought) == 2
    assert bought[1] - bought[0] == 1


def test_best_available_rejects_foreign_held_seats(
    seated_event: SeatedEvent, revel_user: RevelUser, other_user: RevelUser
) -> None:
    """Seats live-held by another identity are never assigned by BEST_AVAILABLE."""
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    held_ids = [seats[0].id, seats[1].id, seats[2].id, seats[3].id]
    holds_service.acquire_seats(event, held_ids, user=other_user, guest_session=None)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assigned = {t.seat_id for t in Ticket.objects.filter(event=event)}
    assert assigned == {seats[4].id, seats[5].id}


def test_best_available_409_when_no_block_fits(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    """Killing seats 1/3/5 fragments the row: no adjacent pair remains → 409."""
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    for seat in (seats[1], seats[3], seats[5]):
        EventSeatOverride.objects.create(event=event, seat=seat, status=EventSeatOverride.OverrideStatus.KILLED)
    with pytest.raises(HttpError) as exc:
        BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assert exc.value.status_code == 409
    assert not Ticket.objects.filter(event=event).exists()


def test_best_available_never_assigns_killed_seat(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    """A KILLED override seat is never ticketed by BEST_AVAILABLE."""
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    EventSeatOverride.objects.create(event=event, seat=seats[0], status=EventSeatOverride.OverrideStatus.KILLED)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event)}
    assert seats[0].id not in assigned
    assert len(assigned) == 2


def test_best_available_purchase_consumes_own_held_block(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    """A buyer's own holds stay in the candidate pool: the purchase assigns the
    held block (here A3/A4, the uniquely central pair) and consumes the holds.
    """
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    holds_service.acquire_seats(event, [seats[2].id, seats[3].id], user=revel_user, guest_session=None)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event)}
    assert assigned == {seats[2].id, seats[3].id}
    assert not SeatHold.objects.filter(event=event).exists()


def test_best_available_own_hold_on_last_pair_succeeds(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    """False-409 regression: the buyer holds the ONLY remaining adjacent pair.

    Excluding the buyer's own holds from the candidates left no block at all and
    raised a spurious 409 on seats the buyer legitimately held.
    """
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    for seat in (seats[1], seats[4]):  # fragment the row: runs [A1], [A3, A4], [A6]
        EventSeatOverride.objects.create(event=event, seat=seat, status=EventSeatOverride.OverrideStatus.KILLED)
    holds_service.acquire_seats(event, [seats[2].id, seats[3].id], user=revel_user, guest_session=None)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])  # must NOT raise 409
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event)}
    assert assigned == {seats[2].id, seats[3].id}
    assert not SeatHold.objects.filter(event=event).exists()


def test_best_available_guest_session_own_hold_succeeds(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    """Guest checkout: the browser's guest-session holds are the buyer's own."""
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    for seat in (seats[1], seats[4]):
        EventSeatOverride.objects.create(event=event, seat=seat, status=EventSeatOverride.OverrideStatus.KILLED)
    holds_service.acquire_seats(event, [seats[2].id, seats[3].id], user=None, guest_session="guest-session-abc")
    service = BatchTicketService(event, tier, revel_user, guest_session="guest-session-abc")
    service.create_batch(items=[_item(), _item()])
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event)}
    assert assigned == {seats[2].id, seats[3].id}
    assert not SeatHold.objects.filter(event=event).exists()


def test_best_available_foreign_hold_on_last_pair_still_409(
    seated_event: SeatedEvent, revel_user: RevelUser, other_user: RevelUser
) -> None:
    """Own-vs-foreign distinction: a FOREIGN hold on the only remaining pair still blocks."""
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    for seat in (seats[1], seats[4]):
        EventSeatOverride.objects.create(event=event, seat=seat, status=EventSeatOverride.OverrideStatus.KILLED)
    holds_service.acquire_seats(event, [seats[2].id, seats[3].id], user=other_user, guest_session=None)
    with pytest.raises(HttpError) as exc:
        BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assert exc.value.status_code == 409
    assert not Ticket.objects.filter(event=event).exists()
    assert SeatHold.objects.filter(event=event).count() == 2  # foreign holds untouched


def test_best_available_checkout_consumes_exact_held_block_over_better_pick(
    seated_event: SeatedEvent, revel_user: RevelUser
) -> None:
    """Checkout must consume the buyer's exact held block, not re-run the picker.

    The buyer holds A1/A2 while A3/A4 is strictly better-scored (centrality 0
    vs 2.0): a re-pick would prefer A3/A4 and strand the original holds until
    TTL. The held-block fast path must return exactly the held seats.
    """
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    holds_service.acquire_seats(event, [seats[0].id, seats[1].id], user=revel_user, guest_session=None)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event)}
    assert assigned == {seats[0].id, seats[1].id}
    assert not SeatHold.objects.filter(event=event).exists()


def test_best_available_checkout_consumes_accessible_held_block_without_flag(
    seated_event: SeatedEvent, revel_user: RevelUser
) -> None:
    """An accessible held block is consumed even when checkout omits accessible_required.

    The buyer held accessible seats (accessible_required hold); the general
    picker pool excludes accessible seats, so a re-pick would assign different
    (non-accessible) seats — the shown block would be unpurchasable.
    """
    from events.service.seating.pick import hold_best_available

    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    VenueSeat.objects.filter(id__in=[seats[4].id, seats[5].id]).update(is_accessible=True)
    result = hold_best_available(event, tier, 2, user=revel_user, guest_session=None, accessible_required=True)
    assert {h.seat_id for h in result.held} == {seats[4].id, seats[5].id}
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])  # no accessible_required
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event)}
    assert assigned == {seats[4].id, seats[5].id}
    assert not SeatHold.objects.filter(event=event).exists()


def test_best_available_partial_hold_falls_through_to_picker(seated_event: SeatedEvent, revel_user: RevelUser) -> None:
    """Fewer held seats than requested → normal picker path, own hold still a candidate.

    Holding only A3 and buying two seats picks the best pair (A3/A4, centrality
    0), which includes the held seat — the hold is consumed, none linger.
    """
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    holds_service.acquire_seats(event, [seats[2].id], user=revel_user, guest_session=None)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event)}
    assert assigned == {seats[2].id, seats[3].id}
    assert not SeatHold.objects.filter(event=event).exists()


def test_best_available_sniped_held_seat_falls_through_to_picker(
    seated_event: SeatedEvent, revel_user: RevelUser, other_user: RevelUser
) -> None:
    """A held seat ticketed post-hold (holds are advisory) → clean fall-through.

    The fast path's post-lock re-check conflicts on the sniped seat; the
    savepoint rollback restores the buyer's holds untouched and the picker
    assigns a clean block instead. No partial state.
    """
    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    holds_service.acquire_seats(event, [seats[0].id, seats[1].id], user=revel_user, guest_session=None)
    Ticket.objects.create(
        event=event,
        tier=tier,
        user=other_user,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name="Sniper",
        seat=seats[0],
        sector=seats[0].sector,
        venue=seats[0].sector.venue,
    )
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event, user=revel_user)}
    assert assigned == {seats[2].id, seats[3].id}  # picker's best clean pair
    # Fast-path savepoint rolled back: the buyer's holds survive untouched (until TTL).
    assert set(SeatHold.objects.filter(event=event).values_list("seat_id", flat=True)) == {
        seats[0].id,
        seats[1].id,
    }


def test_best_available_post_lock_recheck_retries_on_stale_pick(
    seated_event: SeatedEvent, revel_user: RevelUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale pick containing a now-overridden seat is caught by the post-lock
    re-check; the per-attempt savepoint rolls back (releasing that attempt's
    locks) and the retry succeeds on clean seats.
    """
    from events.service.seating import pick as pick_module
    from events.service.seating.best_available import CandidateSeat

    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    EventSeatOverride.objects.create(event=event, seat=seats[0], status=EventSeatOverride.OverrideStatus.KILLED)

    real_load = pick_module.load_candidates
    calls: list[int] = []

    def stale_then_real(
        event_arg: Event, tier_arg: TicketTier, exclude: set[t.Any], **kwargs: t.Any
    ) -> list[CandidateSeat]:
        calls.append(1)
        if len(calls) == 1:
            # Simulate a read taken before the kill: the killed seat looks free.
            return [
                CandidateSeat(
                    id=s.id,
                    row_order=s.row_order,
                    adjacency_index=s.adjacency_index,
                    is_accessible=s.is_accessible,
                    sector_display_order=s.sector.display_order,
                    row_length=max(x.adjacency_index for x in seats) + 1,
                )
                for s in seats[:2]
            ]
        return real_load(event_arg, tier_arg, exclude, **kwargs)

    monkeypatch.setattr(pick_module, "load_candidates", stale_then_real)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event)}
    assert seats[0].id not in assigned
    assert len(assigned) == 2
    assert len(calls) >= 2  # first attempt conflicted post-lock, retry re-picked


def test_best_available_deactivated_seat_conflicts_post_lock(
    seated_event: SeatedEvent, revel_user: RevelUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seat deactivated after the unlocked pick is caught by the is_active
    re-check under the lock (length mismatch) and the retry re-picks without it.
    """
    from events.service.seating import pick as pick_module
    from events.service.seating.best_available import CandidateSeat

    event, seats = seated_event
    tier = _best_available_tier(event, seats)
    VenueSeat.objects.filter(pk=seats[0].pk).update(is_active=False)

    real_load = pick_module.load_candidates
    calls: list[int] = []

    def stale_then_real(
        event_arg: Event, tier_arg: TicketTier, exclude: set[t.Any], **kwargs: t.Any
    ) -> list[CandidateSeat]:
        calls.append(1)
        if len(calls) == 1:
            # Simulate a read taken before the deactivation.
            return [
                CandidateSeat(
                    id=s.id,
                    row_order=s.row_order,
                    adjacency_index=s.adjacency_index,
                    is_accessible=s.is_accessible,
                    sector_display_order=s.sector.display_order,
                    row_length=max(x.adjacency_index for x in seats) + 1,
                )
                for s in seats[:2]
            ]
        return real_load(event_arg, tier_arg, exclude, **kwargs)

    monkeypatch.setattr(pick_module, "load_candidates", stale_then_real)
    BatchTicketService(event, tier, revel_user).create_batch(items=[_item(), _item()])
    assigned = {ticket.seat_id for ticket in Ticket.objects.filter(event=event)}
    assert seats[0].id not in assigned
    assert len(assigned) == 2
    assert len(calls) >= 2
