"""Per-request best-available zone (#749): resolution rules, pool confinement, held-block reuse.

The zone a best-available buyer draws from is a REQUEST parameter, not a tier
attribute. ``resolve_requested_zone`` is the single authority for whether a given
``price_category_id`` is usable on a tier; every path (hold route, authenticated
checkout, guest checkout) goes through it.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import InvalidZoneSelectionError
from events.models import Event, PriceCategory, SeatHold, TicketTier, VenueSeat, VenueSector
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service import BatchTicketService
from events.service.seating import holds as holds_service
from events.service.seating import pick

pytestmark = pytest.mark.django_db

SeatedEvent = tuple[Event, list[VenueSeat]]

ACCESSIBLE_EXHAUSTED_MSG = "Not enough accessible seats available — please contact the organizer."


def _category(event: Event, name: str, color: str = "#00aa00") -> PriceCategory:
    venue = event.venue
    assert venue is not None
    return PriceCategory.objects.create(venue=venue, name=name, color=color)


def _paint(seats: list[VenueSeat], category: PriceCategory) -> None:
    VenueSeat.objects.filter(id__in=[s.id for s in seats]).update(default_price_category=category)


def _tier(
    event: Event,
    sector: VenueSector,
    prices: dict[PriceCategory, str],
    *,
    mode: TicketTier.SeatAssignmentMode = TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
    name: str = "BA",
) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("50.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        sector=sector,
        category_prices={str(c.id): p for c, p in prices.items()},
        seat_assignment_mode=mode,
    )


def _second_sector(event: Event) -> tuple[VenueSector, list[VenueSeat]]:
    """A second seated sector (B1..B4) in the same venue — the cross-sector bleed trap."""
    venue = event.venue
    assert venue is not None
    sector = VenueSector.objects.create(venue=venue, name="Balcony", display_order=1)
    seats = [
        VenueSeat.objects.create(sector=sector, label=f"B{i}", row_label="B", number=i, adjacency_index=i - 1)
        for i in range(1, 5)
    ]
    return sector, seats


def _items(count: int) -> list[TicketPurchaseItem]:
    return [TicketPurchaseItem(guest_name=f"Guest {i}") for i in range(count)]


# --- resolve_requested_zone: the 400 table -----------------------------------


def test_mapped_tier_without_zone_is_rejected_naming_the_zones(seated_event: SeatedEvent) -> None:
    """A tier that prices two zones cannot guess which one the buyer meant."""
    event, seats = seated_event
    stalls, balcony = _category(event, "Stalls"), _category(event, "Balcony", "#aa0000")
    tier = _tier(event, seats[0].sector, {stalls: "40.00", balcony: "20.00"})

    with pytest.raises(InvalidZoneSelectionError) as exc:
        pick.resolve_requested_zone(tier, None)

    assert "Balcony" in str(exc.value)
    assert "Stalls" in str(exc.value)


def test_unknown_zone_is_rejected_naming_the_zones(seated_event: SeatedEvent) -> None:
    event, seats = seated_event
    stalls = _category(event, "Stalls")
    tier = _tier(event, seats[0].sector, {stalls: "40.00"})

    with pytest.raises(InvalidZoneSelectionError) as exc:
        pick.resolve_requested_zone(tier, uuid4())

    assert "Stalls" in str(exc.value)


def test_venue_category_that_is_not_a_zone_of_this_tier_is_rejected(seated_event: SeatedEvent) -> None:
    """Structurally unsellable through this tier is the feature, not an oversight."""
    event, seats = seated_event
    stalls, boxes = _category(event, "Stalls"), _category(event, "Boxes", "#aa0000")
    tier = _tier(event, seats[0].sector, {stalls: "40.00"})

    with pytest.raises(InvalidZoneSelectionError) as exc:
        pick.resolve_requested_zone(tier, boxes.id)

    assert "Stalls" in str(exc.value)
    assert "Boxes" not in str(exc.value)


def test_zone_on_an_unmapped_tier_is_rejected_never_ignored(seated_event: SeatedEvent) -> None:
    """A no-op parameter the buyer believes selected a zone is a money bug."""
    event, seats = seated_event
    stalls = _category(event, "Stalls")
    tier = _tier(event, seats[0].sector, {})

    with pytest.raises(InvalidZoneSelectionError):
        pick.resolve_requested_zone(tier, stalls.id)


def test_zone_on_a_non_best_available_tier_is_rejected(seated_event: SeatedEvent) -> None:
    event, seats = seated_event
    stalls = _category(event, "Stalls")
    tier = _tier(
        event,
        seats[0].sector,
        {stalls: "40.00"},
        mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        name="UC",
    )

    with pytest.raises(InvalidZoneSelectionError):
        pick.resolve_requested_zone(tier, stalls.id)


def test_unmapped_tier_without_zone_resolves_to_the_whole_sector(seated_event: SeatedEvent) -> None:
    event, seats = seated_event
    tier = _tier(event, seats[0].sector, {})

    assert pick.resolve_requested_zone(tier, None) is None


def test_mapped_zone_resolves_to_itself(seated_event: SeatedEvent) -> None:
    event, seats = seated_event
    stalls = _category(event, "Stalls")
    tier = _tier(event, seats[0].sector, {stalls: "40.00"})

    assert pick.resolve_requested_zone(tier, stalls.id) == stalls.id


def test_non_best_available_tier_without_zone_resolves_to_none(seated_event: SeatedEvent) -> None:
    event, seats = seated_event
    stalls = _category(event, "Stalls")
    tier = _tier(
        event,
        seats[0].sector,
        {stalls: "40.00"},
        mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        name="UC",
    )

    assert pick.resolve_requested_zone(tier, None) is None


# --- the pool is the tier's sector, never the venue --------------------------


def test_pool_does_not_bleed_across_sectors_sharing_a_category(seated_event: SeatedEvent) -> None:
    """A category painted in two sectors must not put the other sector's seats in the pool."""
    event, seats = seated_event
    other_sector, other_seats = _second_sector(event)
    shared = _category(event, "Stalls")
    _paint(seats, shared)
    _paint(other_seats, shared)
    tier = _tier(event, seats[0].sector, {shared: "40.00"})

    candidate_ids = {c.id for c in pick.load_candidates(event, tier, set(), zone_id=shared.id)}

    assert candidate_ids == {s.id for s in seats}
    assert candidate_ids.isdisjoint({s.id for s in other_seats})
    assert other_sector.id != tier.sector_id


def test_zone_narrows_the_pool_within_the_sector(seated_event: SeatedEvent) -> None:
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats[:3], front)
    _paint(seats[3:], back)
    tier = _tier(event, seats[0].sector, {front: "40.00", back: "20.00"})

    candidate_ids = {c.id for c in pick.load_candidates(event, tier, set(), zone_id=back.id)}

    assert candidate_ids == {s.id for s in seats[3:]}


def test_hold_best_available_holds_only_seats_of_the_requested_zone(
    seated_event: SeatedEvent, member_user: RevelUser
) -> None:
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats[:2], front)
    _paint(seats[2:], back)
    tier = _tier(event, seats[0].sector, {front: "40.00", back: "20.00"})

    result = pick.hold_best_available(event, tier, 2, user=member_user, guest_session=None, price_category_id=front.id)

    assert {h.seat_id for h in result.held} == {seats[0].id, seats[1].id}


def test_hold_best_available_rejects_a_zone_the_tier_does_not_price(
    seated_event: SeatedEvent, member_user: RevelUser
) -> None:
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats, front)
    tier = _tier(event, seats[0].sector, {front: "40.00"})

    with pytest.raises(InvalidZoneSelectionError):
        pick.hold_best_available(event, tier, 2, user=member_user, guest_session=None, price_category_id=back.id)


# --- accessible refusal is per-zone by construction --------------------------


def test_accessible_refusal_is_scoped_to_the_requested_zone(seated_event: SeatedEvent, member_user: RevelUser) -> None:
    """Accessible seats in ANOTHER zone must not rescue a request for this one.

    No new availability field is needed: the refusal is pick-time and inherits the
    zone-scoped pool, so it becomes per-zone by construction.
    """
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats[:3], front)
    _paint(seats[3:], back)
    VenueSeat.objects.filter(id__in=[s.id for s in seats[3:]]).update(is_accessible=True)
    tier = _tier(event, seats[0].sector, {front: "40.00", back: "20.00"})

    service = BatchTicketService(event, tier, member_user, accessible_required=True, price_category_id=front.id)
    with pytest.raises(HttpError) as exc:
        service.create_batch(items=_items(2))

    assert exc.value.status_code == 409
    assert str(exc.value) == ACCESSIBLE_EXHAUSTED_MSG


def test_accessible_request_succeeds_in_the_zone_that_has_accessible_seats(
    seated_event: SeatedEvent, member_user: RevelUser
) -> None:
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats[:3], front)
    _paint(seats[3:], back)
    VenueSeat.objects.filter(id__in=[s.id for s in seats[3:5]]).update(is_accessible=True)
    tier = _tier(event, seats[0].sector, {front: "40.00", back: "20.00"})

    service = BatchTicketService(event, tier, member_user, accessible_required=True, price_category_id=back.id)
    tickets = service.create_batch(items=_items(2))

    assert isinstance(tickets, list)
    assert {t.seat_id for t in tickets} == {seats[3].id, seats[4].id}


# --- held-block reuse: sector ∩ zone, non-matching holds are invisible -------


def test_held_block_in_another_sector_is_not_consumed(seated_event: SeatedEvent, member_user: RevelUser) -> None:
    event, seats = seated_event
    _, other_seats = _second_sector(event)
    shared = _category(event, "Stalls")
    _paint(seats, shared)
    _paint(other_seats, shared)
    tier = _tier(event, seats[0].sector, {shared: "40.00"})
    holds_service.acquire_seats(event, [other_seats[0].id, other_seats[1].id], user=member_user, guest_session=None)

    tickets = BatchTicketService(event, tier, member_user, price_category_id=shared.id).create_batch(items=_items(2))

    assert isinstance(tickets, list)
    assert {t.seat_id for t in tickets}.issubset({s.id for s in seats})


def test_held_block_in_the_requested_zone_is_consumed_exactly(
    seated_event: SeatedEvent, member_user: RevelUser
) -> None:
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats[:3], front)
    _paint(seats[3:], back)
    tier = _tier(event, seats[0].sector, {front: "40.00", back: "20.00"})
    # The edge pair of the zone — never what the picker would choose on its own.
    holds_service.acquire_seats(event, [seats[4].id, seats[5].id], user=member_user, guest_session=None)

    tickets = BatchTicketService(event, tier, member_user, price_category_id=back.id).create_batch(items=_items(2))

    assert isinstance(tickets, list)
    assert {t.seat_id for t in tickets} == {seats[4].id, seats[5].id}


def test_stale_hold_in_another_zone_does_not_block_a_matching_held_block(
    seated_event: SeatedEvent, member_user: RevelUser
) -> None:
    """The buyer browsed Front, then switched to Back — the hold endpoint only ever ADDS.

    The leftover Front holds must not speak for a checkout that named Back: the Back
    block the buyer was actually shown is what gets consumed.
    """
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats[:3], front)
    _paint(seats[3:], back)
    tier = _tier(event, seats[0].sector, {front: "40.00", back: "20.00"})
    holds_service.acquire_seats(event, [seats[0].id, seats[1].id], user=member_user, guest_session=None)
    # The edge pair of the zone — never what the picker would choose on its own.
    holds_service.acquire_seats(event, [seats[4].id, seats[5].id], user=member_user, guest_session=None)

    tickets = BatchTicketService(event, tier, member_user, price_category_id=back.id).create_batch(items=_items(2))

    assert isinstance(tickets, list)
    assert {t.seat_id for t in tickets} == {seats[4].id, seats[5].id}


def test_holds_only_in_another_zone_fall_through_to_the_picker(
    seated_event: SeatedEvent, member_user: RevelUser
) -> None:
    """Nothing held in the requested zone == nothing the buyer was shown there.

    So this is the no-holds-at-all case: the picker runs. Deliberately NOT a refusal —
    "holds elsewhere block you" would be non-monotonic (holding one seat in the
    requested zone would *fix* the error) and, on the guest path, would only surface
    at email-confirm time on another device with no hold-release UI.
    """
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats[:3], front)
    _paint(seats[3:], back)
    tier = _tier(event, seats[0].sector, {front: "40.00", back: "20.00"})
    holds_service.acquire_seats(event, [seats[0].id, seats[1].id], user=member_user, guest_session=None)

    tickets = BatchTicketService(event, tier, member_user, price_category_id=back.id).create_batch(items=_items(2))

    assert isinstance(tickets, list)
    assert {t.seat_id for t in tickets}.issubset({s.id for s in seats[3:]})
    # The Front holds are untouched — only the purchased zone's holds are consumed.
    assert SeatHold.objects.active().filter(event=event, seat_id=seats[0].id).exists()


def test_partial_match_in_the_requested_zone_falls_through_to_the_picker(
    seated_event: SeatedEvent, member_user: RevelUser
) -> None:
    """Fewer than ``count`` matching held seats is the pre-existing fall-through, zone or not."""
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats[:3], front)
    _paint(seats[3:], back)
    tier = _tier(event, seats[0].sector, {front: "40.00", back: "20.00"})
    holds_service.acquire_seats(event, [seats[0].id, seats[5].id], user=member_user, guest_session=None)

    tickets = BatchTicketService(event, tier, member_user, price_category_id=back.id).create_batch(items=_items(2))

    assert isinstance(tickets, list)
    assert {t.seat_id for t in tickets}.issubset({s.id for s in seats[3:]})


def test_checkout_rejects_a_zone_the_tier_does_not_price(seated_event: SeatedEvent, member_user: RevelUser) -> None:
    event, seats = seated_event
    front, back = _category(event, "Front"), _category(event, "Back", "#aa0000")
    _paint(seats, front)
    tier = _tier(event, seats[0].sector, {front: "40.00"})

    service = BatchTicketService(event, tier, member_user, price_category_id=back.id)
    with pytest.raises(InvalidZoneSelectionError):
        service.create_batch(items=_items(1))


def test_checkout_rejects_a_zone_on_a_general_admission_tier(seated_event: SeatedEvent, member_user: RevelUser) -> None:
    event, seats = seated_event
    front = _category(event, "Front")
    tier = TicketTier.objects.create(
        event=event,
        name="GA",
        price=Decimal("10.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE,
    )

    service = BatchTicketService(event, tier, member_user, price_category_id=front.id)
    with pytest.raises(InvalidZoneSelectionError):
        service.create_batch(items=_items(1))


def test_zone_names_render_in_display_order(seated_event: SeatedEvent) -> None:
    event, seats = seated_event
    back = _category(event, "Back", "#aa0000")
    back.display_order = 1
    back.save(update_fields=["display_order"])
    front = _category(event, "Front")
    tier = _tier(event, seats[0].sector, {front: "40.00", back: "20.00"})

    with pytest.raises(InvalidZoneSelectionError) as exc:
        pick.resolve_requested_zone(tier, None)

    assert str(exc.value).index("Front") < str(exc.value).index("Back")


def test_resolve_returns_the_uuid_it_was_given(seated_event: SeatedEvent) -> None:
    event, seats = seated_event
    front = _category(event, "Front")
    tier = _tier(event, seats[0].sector, {front: "40.00"})

    resolved = pick.resolve_requested_zone(tier, front.id)

    assert isinstance(resolved, UUID)
