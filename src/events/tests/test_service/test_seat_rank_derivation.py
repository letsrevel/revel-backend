"""Tests for server-side row_order/adjacency_index derivation on seat writes."""

import pytest
from ninja.errors import HttpError

from events import schema
from events.models import Organization, Venue, VenueSeat, VenueSector
from events.service import venue_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def venue(organization: Organization) -> Venue:
    return Venue.objects.create(organization=organization, name="Main Hall")


@pytest.fixture
def sector(venue: Venue) -> VenueSector:
    return VenueSector.objects.create(venue=venue, name="Stalls")


def _grid(rows: list[str], numbers: list[int]) -> list[schema.VenueSeatInputSchema]:
    """Grid-editor-shaped payload: labels/rows/numbers only, no explicit ranks."""
    return [
        schema.VenueSeatInputSchema(label=f"{row}{num}", row=row, number=num)  # type: ignore[call-arg]
        for row in rows
        for num in numbers
    ]


class TestDeriveOnBulkCreate:
    def test_ranks_derived_from_rows_and_numbers(self, sector: VenueSector) -> None:
        seats = venue_service.bulk_create_seats(sector, _grid(["A", "B"], [1, 2, 3]))

        by_label = {s.label: s for s in seats}
        assert [by_label[f"A{i}"].adjacency_index for i in (1, 2, 3)] == [0, 1, 2]
        assert [by_label[f"B{i}"].adjacency_index for i in (1, 2, 3)] == [0, 1, 2]
        assert all(by_label[f"A{i}"].row_order == 0 for i in (1, 2, 3))
        assert all(by_label[f"B{i}"].row_order == 1 for i in (1, 2, 3))

    def test_dense_rank_collapses_number_gaps(self, sector: VenueSector) -> None:
        """Printed numbers 1,3,5 dense-rank to adjacency 0,1,2 (migration 0098 semantics)."""
        seats = venue_service.bulk_create_seats(sector, _grid(["A"], [1, 3, 5]))
        assert sorted(s.adjacency_index for s in seats) == [0, 1, 2]

    def test_null_row_goes_to_zero_bucket(self, sector: VenueSector) -> None:
        payload = [
            schema.VenueSeatInputSchema(label="X1", number=1),  # type: ignore[call-arg]
            schema.VenueSeatInputSchema(label="A1", row="A", number=1),  # type: ignore[call-arg]
        ]
        seats = venue_service.bulk_create_seats(sector, payload)
        by_label = {s.label: s for s in seats}
        assert by_label["X1"].row_order == 0
        assert by_label["A1"].row_order == 0  # "A" is the first (only) labeled row

    def test_unnumbered_seats_rank_after_numbered(self, sector: VenueSector) -> None:
        payload = [
            schema.VenueSeatInputSchema(label="A-std", row="A"),  # type: ignore[call-arg]
            schema.VenueSeatInputSchema(label="A2", row="A", number=2),  # type: ignore[call-arg]
            schema.VenueSeatInputSchema(label="A1", row="A", number=1),  # type: ignore[call-arg]
        ]
        seats = venue_service.bulk_create_seats(sector, payload)
        by_label = {s.label: s for s in seats}
        assert by_label["A1"].adjacency_index == 0
        assert by_label["A2"].adjacency_index == 1
        assert by_label["A-std"].adjacency_index == 2

    def test_explicit_ranks_win_wholesale(self, sector: VenueSector) -> None:
        """If ANY seat carries explicit ranks, derivation is skipped for the request."""
        payload = [
            schema.VenueSeatInputSchema(label="A1", row="A", number=1, row_order=7, adjacency_index=9),  # type: ignore[call-arg]
            schema.VenueSeatInputSchema(label="A2", row="A", number=2),  # type: ignore[call-arg]
        ]
        seats = venue_service.bulk_create_seats(sector, payload)
        by_label = {s.label: s for s in seats}
        assert by_label["A1"].row_order == 7
        assert by_label["A1"].adjacency_index == 9
        # A2 keeps the model defaults — no partial derivation
        assert by_label["A2"].row_order == 0
        assert by_label["A2"].adjacency_index == 0

    def test_rerank_covers_whole_sector_on_later_create(self, sector: VenueSector) -> None:
        """Adding a row before existing ones re-ranks the already-persisted seats."""
        venue_service.bulk_create_seats(sector, _grid(["B"], [1, 2]))
        venue_service.bulk_create_seats(sector, _grid(["A"], [1, 2]))

        ranks = dict(sector.seats.values_list("label", "row_order"))
        assert ranks == {"A1": 0, "A2": 0, "B1": 1, "B2": 1}


class TestDeriveOnCreateSector:
    def test_create_sector_with_seats_derives_ranks(self, venue: Venue) -> None:
        payload = schema.VenueSectorCreateSchema(name="Floor", seats=_grid(["A", "B"], [1, 2]))  # type: ignore[call-arg]
        sector = venue_service.create_sector(venue, payload)

        ranks = {s.label: (s.row_order, s.adjacency_index) for s in sector.seats.all()}
        assert ranks == {"A1": (0, 0), "A2": (0, 1), "B1": (1, 0), "B2": (1, 1)}


class TestDeriveOnUpdates:
    def test_update_seat_row_change_reranks_sector(self, sector: VenueSector) -> None:
        venue_service.bulk_create_seats(sector, _grid(["A", "B"], [1, 2]))
        seat = sector.seats.get(label="B1")

        updated = venue_service.update_seat(seat, schema.VenueSeatUpdateSchema(row="A", number=3))  # type: ignore[call-arg]

        assert updated.row_order == 0
        assert updated.adjacency_index == 2
        # B2 is now the only seat of row B, re-ranked accordingly
        b2 = sector.seats.get(label="B2")
        assert b2.row_order == 1
        assert b2.adjacency_index == 0

    def test_update_seat_without_order_fields_keeps_ranks(self, sector: VenueSector) -> None:
        """A non-ordering update (e.g. accessibility) must not touch existing ranks."""
        venue_service.bulk_create_seats(sector, _grid(["A"], [1, 2]))
        seat = sector.seats.get(label="A2")
        VenueSeat.objects.filter(pk=seat.pk).update(adjacency_index=5)  # designer-set rank
        seat.refresh_from_db()

        updated = venue_service.update_seat(seat, schema.VenueSeatUpdateSchema(is_accessible=True))  # type: ignore[call-arg]

        assert updated.adjacency_index == 5

    def test_update_seat_explicit_ranks_win(self, sector: VenueSector) -> None:
        venue_service.bulk_create_seats(sector, _grid(["A"], [1, 2]))
        seat = sector.seats.get(label="A1")

        updated = venue_service.update_seat(
            seat,
            schema.VenueSeatUpdateSchema(number=9, row_order=3, adjacency_index=4),  # type: ignore[call-arg]
        )

        assert updated.row_order == 3
        assert updated.adjacency_index == 4

    def test_bulk_update_row_change_reranks_sector(self, sector: VenueSector) -> None:
        venue_service.bulk_create_seats(sector, _grid(["A", "B"], [1, 2]))

        updated = venue_service.bulk_update_seats(
            sector,
            [schema.VenueSeatBulkUpdateItemSchema(label="A2", number=5)],  # type: ignore[call-arg]
        )

        assert updated[0].adjacency_index == 1  # dense rank: numbers 1,5 → 0,1
        ranks = {s.label: (s.row_order, s.adjacency_index) for s in sector.seats.all()}
        assert ranks == {"A1": (0, 0), "A2": (0, 1), "B1": (1, 0), "B2": (1, 1)}

    def test_bulk_update_explicit_ranks_win_wholesale(self, sector: VenueSector) -> None:
        venue_service.bulk_create_seats(sector, _grid(["A"], [1, 2]))

        updated = venue_service.bulk_update_seats(
            sector,
            [
                schema.VenueSeatBulkUpdateItemSchema(label="A1", number=9, adjacency_index=8),  # type: ignore[call-arg]
                schema.VenueSeatBulkUpdateItemSchema(label="A2", number=1),  # type: ignore[call-arg]
            ],
        )

        by_label = {s.label: s for s in updated}
        assert by_label["A1"].adjacency_index == 8
        assert by_label["A2"].adjacency_index == 1  # untouched by derivation (explicit wins wholesale)


class TestSectorKindUpdate:
    def test_kind_change_allowed_with_no_seats(self, sector: VenueSector) -> None:
        updated = venue_service.update_sector(
            sector,
            schema.VenueSectorUpdateSchema(kind=VenueSector.Kind.STANDING),  # type: ignore[call-arg]
        )
        assert updated.kind == VenueSector.Kind.STANDING

    def test_kind_change_rejected_with_seats(self, sector: VenueSector) -> None:
        VenueSeat.objects.create(sector=sector, label="A1")
        with pytest.raises(HttpError) as exc_info:
            venue_service.update_sector(
                sector,
                schema.VenueSectorUpdateSchema(kind=VenueSector.Kind.STANDING),  # type: ignore[call-arg]
            )
        assert exc_info.value.status_code == 400
        sector.refresh_from_db()
        assert sector.kind == VenueSector.Kind.SEATED

    def test_same_kind_noop_allowed_with_seats(self, sector: VenueSector) -> None:
        VenueSeat.objects.create(sector=sector, label="A1")
        updated = venue_service.update_sector(
            sector,
            schema.VenueSectorUpdateSchema(kind=VenueSector.Kind.SEATED, name="Renamed"),  # type: ignore[call-arg]
        )
        assert updated.kind == VenueSector.Kind.SEATED
        assert updated.name == "Renamed"
