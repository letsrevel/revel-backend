"""Pure scoring tests — no DB."""

import typing as t
import uuid

from events.service.seating.best_available import CandidateSeat, pick_best_available


def _row(
    row_order: int, count: int, taken: t.AbstractSet[int] = frozenset(), accessible: t.AbstractSet[int] = frozenset()
) -> list[CandidateSeat]:
    return [
        CandidateSeat(
            id=uuid.uuid4(),
            row_order=row_order,
            adjacency_index=i,
            is_accessible=i in accessible,
            sector_display_order=0,
        )
        for i in range(count)
        if i not in taken
    ]


def test_prefers_front_row_center() -> None:
    front, back = _row(0, 10), _row(1, 10)
    picked = pick_best_available(front + back, 2, seed=1)
    by_id = {s.id: s for s in front + back}
    assert all(by_id[p].row_order == 0 for p in picked)
    adjacency = sorted(by_id[p].adjacency_index for p in picked)
    assert adjacency[1] - adjacency[0] == 1  # contiguous
    assert 3 <= adjacency[0] <= 5  # centered in a 10-seat row


def test_no_stranded_single() -> None:
    # Row with a free run of exactly 3: picking 2 must not leave a stranded single
    # when an alternative run of 2 exists elsewhere.
    row0 = _row(0, 5, taken={0, 4})  # free: 1,2,3  (run of 3)
    row1 = _row(1, 4, taken={0, 3})  # free: 1,2    (run of 2)
    picked = pick_best_available(row0 + row1, 2, seed=1)
    by_id = {s.id: s for s in row0 + row1}
    assert all(by_id[p].row_order == 1 for p in picked)  # takes the exact-fit run


def test_returns_empty_when_no_contiguous_block() -> None:
    row = _row(0, 6, taken={1, 3, 5})  # singles only
    assert pick_best_available(row, 2, seed=1) == []


def test_accessible_seats_protected_from_general_sale() -> None:
    row = _row(0, 4, accessible={0, 1, 2, 3})
    assert pick_best_available(row, 2, seed=1) == []  # nothing non-accessible
    picked = pick_best_available(row, 2, accessible_required=True, seed=1)
    assert len(picked) == 2


def test_accessible_required_relaxed_contiguity() -> None:
    row = _row(0, 6, taken={1}, accessible={0, 2})
    picked = pick_best_available(row, 2, accessible_required=True, seed=1)
    assert len(picked) == 2  # 0 and 2 despite the gap


def test_deterministic_for_fixed_seed() -> None:
    rows = _row(0, 10) + _row(1, 10)
    assert pick_best_available(rows, 3, seed=42) == pick_best_available(rows, 3, seed=42)


def test_quantity_one_picks_centered_front_row() -> None:
    front, back = _row(0, 10), _row(1, 10)
    picked = pick_best_available(front + back, 1, seed=1)
    by_id = {s.id: s for s in front + back}
    assert len(picked) == 1
    assert by_id[picked[0]].row_order == 0
    assert 3 <= by_id[picked[0]].adjacency_index <= 6


def test_party_larger_than_any_row_returns_empty() -> None:
    row = _row(0, 4)
    assert pick_best_available(row, 5, seed=1) == []
