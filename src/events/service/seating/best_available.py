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
    # Full physical row length (max adjacency_index over ALL active seats in the row,
    # + 1) — NOT the available pool's extent. Centrality is scored against the real
    # row midpoint, so sold/held high-index seats don't shift the perceived center.
    row_length: int
    # Identifies the physical row alongside row_order. The seat designer currently
    # writes row_order=0 for every editor-built seat, so row_order alone would collapse
    # distinct rows onto one adjacency plane; row_label keeps them apart (rows are grouped
    # by (sector_display_order, row_order, row_label)). Adding it to the key only ever
    # SPLITS groups, so the row_order-distinguished path is unchanged.
    row_label: str | None = None


def _contiguous_runs(seats: list[CandidateSeat]) -> list[list[CandidateSeat]]:
    """Split one row's seats (sorted by adjacency_index) into contiguous runs."""
    ordered = sorted(seats, key=lambda s: s.adjacency_index)
    runs: list[list[CandidateSeat]] = []
    for _, group in itertools.groupby(enumerate(ordered), key=lambda pair: pair[1].adjacency_index - pair[0]):
        runs.append([seat for _, seat in group])
    return runs


def _placement_score(run: list[CandidateSeat], start: int, quantity: int, row_len_hint: int) -> tuple[float, ...]:
    """Score a candidate placement: lower is better.

    Order (spec §3): row_order (front rows first) > centrality (closer to the row's
    midpoint) > fragmentation penalty > sector_display_order. Row rank dominates:
    fragmentation is a *within-row* tiebreak that never demotes a party across rows,
    so a front-row run that strands a single always beats a back-row exact-fit run.
    Among same-row equally-central placements it avoids stranding a single leftover
    seat on either side of the run (keeps the hall sellable late in the on-sale).
    """
    seats = run[start : start + quantity]
    row_order = seats[0].row_order
    midpoint = (seats[0].adjacency_index + seats[-1].adjacency_index) / 2
    centrality = abs(midpoint - (row_len_hint - 1) / 2)
    leftover_left = start
    leftover_right = len(run) - (start + quantity)
    fragmentation = (1 if leftover_left == 1 else 0) + (1 if leftover_right == 1 else 0)
    return (row_order, centrality, fragmentation, seats[0].sector_display_order)


def _pick_general(pool: list[CandidateSeat], quantity: int, seed: int | None) -> list[uuid.UUID]:
    # Keyed by (sector, row_order, row_label): row_label breaks the tie when the designer
    # left row_order=0 on every seat, so seats from different rows never merge into one
    # contiguous-run plane. Contiguity is still scored on adjacency_index within a row.
    rows: dict[tuple[int, int, str | None], list[CandidateSeat]] = {}
    for s in pool:
        rows.setdefault((s.sector_display_order, s.row_order, s.row_label), []).append(s)
    # Full-row bounds carried by the candidates themselves — never derived from the
    # (already filtered) pool, which would move the midpoint when edge seats are taken.
    row_len = {key: max(s.row_length for s in seats) for key, seats in rows.items()}

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
    # Only genuinely-equivalent placements are shuffled by the seeded RNG: same row,
    # same fragmentation, same sector, centrality within half a seat. The randomizer
    # must never pick a worse row or a stranded-single option over a non-stranding
    # equally-central one — those differ on a dominant key and are excluded here.
    near_equal = [
        p
        for p in placements
        if p[0][0] == best_score[0]  # same row_order
        and abs(p[0][1] - best_score[1]) <= 0.5  # centrality within half a seat
        and p[0][2] == best_score[2]  # equal fragmentation
        and p[0][3] == best_score[3]  # equal sector_display_order
    ]
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
    """Return the best contiguous block of `quantity` seat ids, or [] if none exists.

    Accessible seats are protected unconditionally: a general request
    (``accessible_required=False``) is scored over non-accessible seats ONLY and
    never falls back to accessible seats even when the non-accessible pool is
    exhausted — a general party that can't be seated returns ``[]`` (surfaced as
    "not enough adjacent seats") rather than being handed a wheelchair space. This
    is deliberate; do not add an exhaustion fallback. Accessible seats are reachable
    only via ``accessible_required=True``.
    """
    if quantity <= 0:
        return []
    if accessible_required:
        return _pick_accessible([s for s in candidates if s.is_accessible], quantity)
    return _pick_general([s for s in candidates if not s.is_accessible], quantity, seed)
