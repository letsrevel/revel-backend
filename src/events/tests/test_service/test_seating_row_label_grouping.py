"""Row grouping falls back to ``row_label`` when ``row_order`` is uniformly zero.

The frontend seat designer currently writes ``row_order=0`` for every editor-built
seat, so rows are distinguished only by ``row_label``. Grouping on ``row_order`` alone
would collapse every row onto one adjacency plane — the picker would treat seats in
different physical rows as neighbours and the availability counter would run its
"longest contiguous block" across rows. The read paths key rows by
``(sector_display_order, row_order, row_label)`` so this data shape behaves sensibly
without any change to the write contract.
"""

import uuid

import pytest

from events.models import Event, Organization, PriceCategory, TicketTier, Venue, VenueSeat, VenueSector
from events.service.seating.availability import build_zone_availability
from events.service.seating.best_available import CandidateSeat, pick_best_available
from events.service.seating.pick import load_candidates, load_taken_seats


def _candidate(row_label: str, adjacency_index: int, row_length: int) -> CandidateSeat:
    """A non-accessible candidate in row ``row_label`` — every row shares row_order=0."""
    return CandidateSeat(
        id=uuid.uuid4(),
        row_order=0,
        adjacency_index=adjacency_index,
        is_accessible=False,
        sector_display_order=0,
        row_length=row_length,
        row_label=row_label,
    )


def test_pick_stays_within_one_row_when_row_order_is_all_zero() -> None:
    """Two rows, both row_order=0, distinguished only by label — the pick must not span them.

    Without row_label in the grouping key both rows collapse into one plane: their
    adjacency indexes (0..4 and 0..4) interleave, the contiguous-run detection sees
    duplicate positions, and the picker can hand out seats from two different physical
    rows as an "adjacent" block. Keyed by label, each row is its own clean run of five.
    """
    row_a = [_candidate("A", i, 5) for i in range(5)]
    row_b = [_candidate("B", i, 5) for i in range(5)]
    pool = row_a + row_b
    by_id = {c.id: c for c in pool}

    picked = pick_best_available(pool, 4, seed=1)

    assert len(picked) == 4
    labels = {by_id[p].row_label for p in picked}
    assert labels in ({"A"}, {"B"}), "the block must come from a single physical row"
    adjacency = sorted(by_id[p].adjacency_index for p in picked)
    assert adjacency[-1] - adjacency[0] == 3, "and it must be genuinely contiguous within that row"
    assert all(b - a == 1 for a, b in zip(adjacency, adjacency[1:]))


# --- DB integration: load_candidates + build_zone_availability --------------

pytestmark = pytest.mark.django_db


@pytest.fixture
def flat_layout(organization: Organization, event: Event) -> tuple[Event, TicketTier, PriceCategory]:
    """A sector whose two rows share row_order=0 and differ only by row_label.

    Row A: A1..A5, row B: B1..B5, all painted the one category, adjacency 0..4 each.
    Mirrors exactly what the current seat designer writes.
    """
    venue = Venue.objects.create(organization=organization, name="Flat Hall", capacity=100)
    sector = VenueSector.objects.create(venue=venue, name="Stalls")
    category = PriceCategory.objects.create(venue=venue, name="Standard", color="#00aa00")
    for label in ("A", "B"):
        for i in range(5):
            VenueSeat.objects.create(
                sector=sector,
                label=f"{label}{i + 1}",
                row_label=label,
                number=i + 1,
                row_order=0,
                adjacency_index=i,
                is_active=True,
                default_price_category=category,
            )
    tier = TicketTier.objects.create(
        event=event,
        name="Best Available Stalls",
        price="20.00",
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        total_quantity=50,
        max_tickets_per_user=5,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        venue=venue,
        sector=sector,
        category_prices={str(category.pk): "20.00"},
    )
    event.venue = venue
    event.save(update_fields=["venue"])
    return event, tier, category


def test_zone_availability_block_is_per_row_not_merged(
    flat_layout: tuple[Event, TicketTier, PriceCategory],
) -> None:
    """largest_contiguous_block is a single row's five, never a cross-row artifact.

    Ten free seats across two rows of five: merging the rows onto one plane would
    interleave [0,1,2,3,4,0,1,2,3,4] and report a longest run of 2, blocking a legit
    5-seat request. Keyed by label the answer is 5 — the true longest run in a row.
    """
    event, _tier, category = flat_layout
    zones = build_zone_availability(event, load_taken_seats(event).union())

    assert len(zones) == 1
    zone = zones[0]
    assert zone.free_seats == 10
    assert zone.largest_contiguous_block == 5


def test_best_available_pick_over_loaded_candidates_stays_in_one_row(
    flat_layout: tuple[Event, TicketTier, PriceCategory],
) -> None:
    """End to end through the DB loader: a 5-seat pick comes from one physical row."""
    event, tier, _category = flat_layout
    candidates = load_candidates(event, tier, set())
    by_id = {c.id: c for c in candidates}

    picked = pick_best_available(candidates, 5, seed=1)

    assert len(picked) == 5
    assert len({by_id[p].row_label for p in picked}) == 1, "one physical row, not a cross-row block"
    # Each loaded candidate's row_length is its own row's five, not the merged ten.
    assert all(by_id[p].row_length == 5 for p in picked)
