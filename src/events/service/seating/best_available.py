"""Best-available seat picking: contiguous-run scoring (spec §3).

Pure functions — the caller supplies the candidate set (already excluding sold/
held/blocked seats) and locks only the returned seats (optimistic pick).
"""

import dataclasses
import itertools
import random
import uuid


@dataclasses.dataclass(frozen=True)
class CandidateSeat:
    id: uuid.UUID
    row_order: int
    adjacency_index: int
    is_accessible: bool
    sector_display_order: int


def _contiguous_runs(seats: list[CandidateSeat]) -> list[list[CandidateSeat]]:
    """Split one row's seats (sorted by adjacency_index) into contiguous runs."""
    ordered = sorted(seats, key=lambda s: s.adjacency_index)
    runs: list[list[CandidateSeat]] = []
    for _, group in itertools.groupby(enumerate(ordered), key=lambda pair: pair[1].adjacency_index - pair[0]):
        runs.append([seat for _, seat in group])
    return runs


def _placement_score(run: list[CandidateSeat], start: int, quantity: int, row_len_hint: int) -> tuple[float, ...]:
    """Score a candidate placement: lower is better.

    Order: fragmentation penalty (avoid stranding a single leftover seat on either
    side of the run — an exact-fit run elsewhere always beats a partial one that
    strands a single seat) > row_order (front rows first) > centrality (closer to
    the row's midpoint) > sector_display_order (stable tiebreak before randomization).
    """
    seats = run[start : start + quantity]
    row_order = seats[0].row_order
    midpoint = (seats[0].adjacency_index + seats[-1].adjacency_index) / 2
    centrality = abs(midpoint - (row_len_hint - 1) / 2)
    leftover_left = start
    leftover_right = len(run) - (start + quantity)
    fragmentation = (1 if leftover_left == 1 else 0) + (1 if leftover_right == 1 else 0)
    return (fragmentation, row_order, centrality, seats[0].sector_display_order)


def _pick_general(pool: list[CandidateSeat], quantity: int, seed: int | None) -> list[uuid.UUID]:
    rows: dict[tuple[int, int], list[CandidateSeat]] = {}
    for s in pool:
        rows.setdefault((s.sector_display_order, s.row_order), []).append(s)
    row_len = {key: max(s.adjacency_index for s in seats) + 1 for key, seats in rows.items()}

    placements: list[tuple[tuple[float, ...], list[CandidateSeat]]] = []
    for key, seats in rows.items():
        for run in _contiguous_runs(seats):
            for start in range(0, len(run) - quantity + 1):
                score = _placement_score(run, start, quantity, row_len[key])
                placements.append((score, run[start : start + quantity]))

    if not placements:
        return []

    placements.sort(key=lambda p: p[0])
    best_score = placements[0][0]
    # Near-equal placements (same row + fragmentation, centrality within half a seat)
    # are shuffled by the seeded RNG so the "best" pick isn't always identical when
    # several seats are equally good.
    near_equal = [p for p in placements if p[0][:2] == best_score[:2] and abs(p[0][2] - best_score[2]) <= 0.5]
    rng = random.Random(seed)
    chosen = rng.choice(near_equal)
    return [s.id for s in chosen[1]]


def _pick_accessible(pool: list[CandidateSeat], quantity: int) -> list[uuid.UUID]:
    """Accessible-required: contiguity is relaxed — just take the nearest-row seats."""
    ordered = sorted(pool, key=lambda s: (s.row_order, s.sector_display_order, s.adjacency_index))
    if len(ordered) < quantity:
        return []
    return [s.id for s in ordered[:quantity]]


def pick_best_available(
    candidates: list[CandidateSeat],
    quantity: int,
    *,
    accessible_required: bool = False,
    seed: int | None = None,
) -> list[uuid.UUID]:
    """Return the best contiguous block of `quantity` seat ids, or [] if none exists."""
    if accessible_required:
        return _pick_accessible([s for s in candidates if s.is_accessible], quantity)
    return _pick_general([s for s in candidates if not s.is_accessible], quantity, seed)
