"""Tests for the venue service layer."""

from datetime import timedelta

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events import schema
from events.models import Event, Organization, Ticket, TicketTier, Venue, VenueSeat, VenueSector
from events.service import venue_service

pytestmark = pytest.mark.django_db


class TestCreateVenue:
    """Tests for venue creation."""

    def test_create_venue_minimal(self, organization: Organization) -> None:
        """Test creating a venue with minimal data."""
        payload = schema.VenueCreateSchema(name="Test Venue")  # type: ignore[call-arg]
        venue = venue_service.create_venue(organization, payload)

        assert venue.name == "Test Venue"
        assert venue.organization == organization
        assert venue.address is None
        assert venue.capacity is None

    def test_create_venue_full(self, organization: Organization) -> None:
        """Test creating a venue with all fields."""
        payload = schema.VenueCreateSchema(
            name="Grand Theater",
            address="123 Main St",
            capacity=500,
        )
        venue = venue_service.create_venue(organization, payload)

        assert venue.name == "Grand Theater"
        assert venue.address == "123 Main St"
        assert venue.capacity == 500


class TestUpdateVenue:
    """Tests for venue updates."""

    def test_update_venue_name(self, organization: Organization) -> None:
        """Test updating a venue name."""
        venue = Venue.objects.create(organization=organization, name="Old Name")
        payload = schema.VenueUpdateSchema(name="New Name")  # type: ignore[call-arg]

        updated = venue_service.update_venue(venue, payload)

        assert updated.name == "New Name"
        venue.refresh_from_db()
        assert venue.name == "New Name"

    def test_update_venue_no_changes(self, organization: Organization) -> None:
        """Test update with no changes returns venue unchanged."""
        venue = Venue.objects.create(organization=organization, name="Test Venue")
        payload = schema.VenueUpdateSchema()  # type: ignore[call-arg]

        updated = venue_service.update_venue(venue, payload)

        assert updated.name == "Test Venue"


class TestCreateSector:
    """Tests for sector creation."""

    def test_create_sector_minimal(self, organization: Organization) -> None:
        """Test creating a sector with minimal data."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        payload = schema.VenueSectorCreateSchema(name="Orchestra")  # type: ignore[call-arg]

        sector = venue_service.create_sector(venue, payload)

        assert sector.name == "Orchestra"
        assert sector.venue == venue
        assert sector.seats.count() == 0

    def test_create_sector_with_seats(self, organization: Organization) -> None:
        """Test creating a sector with nested seats."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        payload = schema.VenueSectorCreateSchema(
            name="VIP",
            seats=[
                schema.VenueSeatInputSchema(label="A1", row="A", number=1),  # type: ignore[call-arg]
                schema.VenueSeatInputSchema(label="A2", row="A", number=2),  # type: ignore[call-arg]
            ],
        )

        sector = venue_service.create_sector(venue, payload)

        assert sector.name == "VIP"
        assert sector.seats.count() == 2
        assert sector.seats.filter(label="A1").exists()
        assert sector.seats.filter(label="A2").exists()

    def test_create_sector_with_shape_and_valid_positions(self, organization: Organization) -> None:
        """Test creating a sector with shape and valid seat positions."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        shape = [
            schema.Coordinate2D(x=0, y=0),
            schema.Coordinate2D(x=100, y=0),
            schema.Coordinate2D(x=100, y=100),
            schema.Coordinate2D(x=0, y=100),
        ]
        payload = schema.VenueSectorCreateSchema(
            name="Floor",
            shape=shape,
            seats=[
                schema.VenueSeatInputSchema(label="A1", position=schema.Coordinate2D(x=50, y=50)),  # type: ignore[call-arg]
            ],
        )

        sector = venue_service.create_sector(venue, payload)

        assert sector.seats.count() == 1


class TestUpdateSector:
    """Tests for sector updates."""

    def test_update_sector_name(self, organization: Organization) -> None:
        """Test updating a sector name."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Old Name")
        payload = schema.VenueSectorUpdateSchema(name="New Name")  # type: ignore[call-arg]

        updated = venue_service.update_sector(sector, payload)

        assert updated.name == "New Name"
        sector.refresh_from_db()
        assert sector.name == "New Name"

    def test_update_sector_no_changes(self, organization: Organization) -> None:
        """Test update with no changes returns sector unchanged."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Test Sector")
        payload = schema.VenueSectorUpdateSchema()  # type: ignore[call-arg]

        updated = venue_service.update_sector(sector, payload)

        assert updated.name == "Test Sector"

    def test_update_sector_does_not_affect_seats(self, organization: Organization) -> None:
        """Test that updating sector metadata does not affect existing seats."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="VIP", capacity=100)
        VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")

        payload = schema.VenueSectorUpdateSchema(name="Premium", capacity=200)  # type: ignore[call-arg]
        venue_service.update_sector(sector, payload)

        assert sector.seats.count() == 2
        assert sector.seats.filter(label="A1").exists()


class TestBulkCreateSeats:
    """Tests for bulk seat creation."""

    def test_bulk_create_seats(self, organization: Organization) -> None:
        """Test bulk creating seats."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        seats_data = [
            schema.VenueSeatInputSchema(label="A1", row="A", number=1),  # type: ignore[call-arg]
            schema.VenueSeatInputSchema(label="A2", row="A", number=2),  # type: ignore[call-arg]
            schema.VenueSeatInputSchema(label="A3", row="A", number=3, is_accessible=True),  # type: ignore[call-arg]
        ]

        created_seats = venue_service.bulk_create_seats(sector, seats_data)

        assert len(created_seats) == 3
        assert sector.seats.count() == 3
        assert sector.seats.filter(is_accessible=True).count() == 1

    def test_bulk_create_seats_empty_list(self, organization: Organization) -> None:
        """Test bulk create with empty list returns empty."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        created_seats = venue_service.bulk_create_seats(sector, [])

        assert created_seats == []
        assert sector.seats.count() == 0

    def test_bulk_create_seats_validates_positions(self, organization: Organization) -> None:
        """Test that bulk create validates seat positions against sector shape."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )

        seats_data = [
            schema.VenueSeatInputSchema(label="A1", position=schema.Coordinate2D(x=50, y=50)),  # type: ignore[call-arg]  # Inside
            schema.VenueSeatInputSchema(label="A2", position=schema.Coordinate2D(x=150, y=50)),  # type: ignore[call-arg]  # Outside
        ]

        with pytest.raises(HttpError) as exc_info:
            venue_service.bulk_create_seats(sector, seats_data)

        assert exc_info.value.status_code == 400
        assert "outside the sector shape" in str(exc_info.value.message)

    def test_bulk_create_seats_no_shape_skips_validation(self, organization: Organization) -> None:
        """Test that positions are not validated when sector has no shape."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Floor")  # No shape

        seats_data = [
            schema.VenueSeatInputSchema(label="A1", position=schema.Coordinate2D(x=9999, y=9999)),  # type: ignore[call-arg]
        ]

        created_seats = venue_service.bulk_create_seats(sector, seats_data)

        assert len(created_seats) == 1


class TestGetSeatByLabel:
    """Tests for getting a seat by label."""

    def test_get_seat_by_label_found(self, organization: Organization) -> None:
        """Test getting an existing seat."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        found = venue_service.get_seat_by_label(sector, "A1")

        assert found.id == seat.id

    def test_get_seat_by_label_not_found(self, organization: Organization) -> None:
        """Test getting a non-existent seat raises error."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        with pytest.raises(HttpError) as exc_info:
            venue_service.get_seat_by_label(sector, "NONEXISTENT")

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value.message)


class TestUpdateSeat:
    """Tests for seat updates."""

    def test_update_seat_properties(self, organization: Organization) -> None:
        """Test updating seat properties."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1", is_accessible=False)

        payload = schema.VenueSeatUpdateSchema(is_accessible=True, is_obstructed_view=True)  # type: ignore[call-arg]
        updated = venue_service.update_seat(seat, payload)

        assert updated.is_accessible is True
        assert updated.is_obstructed_view is True

    def test_update_seat_position_validates_shape(self, organization: Organization) -> None:
        """Test that position update validates against sector shape."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector_shape: list[dict[str, float]] = [
            {"x": 0.0, "y": 0.0},
            {"x": 100.0, "y": 0.0},
            {"x": 100.0, "y": 100.0},
            {"x": 0.0, "y": 100.0},
        ]
        sector = VenueSector.objects.create(venue=venue, name="Floor", shape=sector_shape)
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        payload = schema.VenueSeatUpdateSchema(position=schema.Coordinate2D(x=150, y=50))  # type: ignore[call-arg]

        with pytest.raises(HttpError) as exc_info:
            venue_service.update_seat(seat, payload, sector_shape=sector_shape)

        assert exc_info.value.status_code == 400
        assert "outside the sector shape" in str(exc_info.value.message)

    def test_update_seat_no_changes(self, organization: Organization) -> None:
        """Test update with no changes returns seat unchanged."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1", row="A")

        payload = schema.VenueSeatUpdateSchema()  # type: ignore[call-arg]
        updated = venue_service.update_seat(seat, payload)

        assert updated.row == "A"


class TestDeleteSeat:
    """Tests for seat deletion."""

    def test_delete_seat_no_tickets(self, organization: Organization) -> None:
        """Test deleting a seat with no tickets."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        seat_id = seat.id

        venue_service.delete_seat(seat)

        assert not VenueSeat.objects.filter(id=seat_id).exists()

    def test_delete_seat_blocked_by_active_future_ticket(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with active ticket for future event cannot be deleted."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.ACTIVE,
        )

        with pytest.raises(HttpError) as exc_info:
            venue_service.delete_seat(seat)

        assert exc_info.value.status_code == 400
        assert "active or pending tickets" in str(exc_info.value.message)
        assert VenueSeat.objects.filter(id=seat.id).exists()

    def test_delete_seat_blocked_by_pending_future_ticket(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with pending ticket for future event cannot be deleted."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.PENDING,
        )

        with pytest.raises(HttpError) as exc_info:
            venue_service.delete_seat(seat)

        assert exc_info.value.status_code == 400
        assert "active or pending tickets" in str(exc_info.value.message)

    def test_delete_seat_allowed_with_cancelled_ticket(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with cancelled ticket can be deleted."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        seat_id = seat.id

        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.CANCELLED,
        )

        venue_service.delete_seat(seat)

        assert not VenueSeat.objects.filter(id=seat_id).exists()

    def test_delete_seat_allowed_with_past_event_ticket(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with ticket for past event can be deleted."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        seat_id = seat.id

        past_event = Event.objects.create(
            organization=organization,
            name="Past Concert",
            start=timezone.now() - timedelta(days=7),
            end=timezone.now() - timedelta(days=7) + timedelta(hours=3),
            status=Event.EventStatus.CLOSED,
        )
        tier = TicketTier.objects.create(event=past_event, name="General", price=50)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=past_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.ACTIVE,
        )

        venue_service.delete_seat(seat)

        assert not VenueSeat.objects.filter(id=seat_id).exists()

    def test_delete_seat_allowed_with_checked_in_ticket(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with checked_in ticket can be deleted (they've already used it)."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        seat_id = seat.id

        future_event = Event.objects.create(
            organization=organization,
            name="Ongoing Concert",
            start=timezone.now() - timedelta(hours=1),
            end=timezone.now() + timedelta(hours=2),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.CHECKED_IN,
            checked_in_at=timezone.now(),
        )

        venue_service.delete_seat(seat)

        assert not VenueSeat.objects.filter(id=seat_id).exists()


class TestBulkDeleteSeats:
    """Tests for bulk seat deletion."""

    def test_bulk_delete_seats_success(self, organization: Organization) -> None:
        """Test bulk deleting seats successfully."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")
        VenueSeat.objects.create(sector=sector, label="A3")

        deleted_count = venue_service.bulk_delete_seats(sector, ["A1", "A2"])

        assert deleted_count == 2
        assert sector.seats.count() == 1
        assert sector.seats.filter(label="A3").exists()

    def test_bulk_delete_seats_empty_list(self, organization: Organization) -> None:
        """Test bulk delete with empty list returns 0."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")

        deleted_count = venue_service.bulk_delete_seats(sector, [])

        assert deleted_count == 0
        assert sector.seats.count() == 1

    def test_bulk_delete_seats_not_found(self, organization: Organization) -> None:
        """Test bulk delete with non-existent seats raises error."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")

        with pytest.raises(HttpError) as exc_info:
            venue_service.bulk_delete_seats(sector, ["A1", "NONEXISTENT", "ALSO_MISSING"])

        assert exc_info.value.status_code == 404
        assert "ALSO_MISSING" in str(exc_info.value.message)
        assert "NONEXISTENT" in str(exc_info.value.message)
        # A1 should still exist - atomic rollback
        assert sector.seats.filter(label="A1").exists()

    def test_bulk_delete_seats_blocked_by_ticket(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test bulk delete blocked when any seat has blocking ticket."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat1 = VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")
        VenueSeat.objects.create(sector=sector, label="A3")

        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat1,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.ACTIVE,
        )

        with pytest.raises(HttpError) as exc_info:
            venue_service.bulk_delete_seats(sector, ["A1", "A2"])

        assert exc_info.value.status_code == 400
        assert "A1" in str(exc_info.value.message)
        # Both seats should still exist - atomic rollback
        assert sector.seats.count() == 3

    def test_bulk_delete_seats_multiple_blocking_tickets(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test bulk delete shows all seats with blocking tickets."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat1 = VenueSeat.objects.create(sector=sector, label="A1")
        seat2 = VenueSeat.objects.create(sector=sector, label="A2")
        VenueSeat.objects.create(sector=sector, label="A3")

        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat1,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.ACTIVE,
        )
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat2,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.PENDING,
        )

        with pytest.raises(HttpError) as exc_info:
            venue_service.bulk_delete_seats(sector, ["A1", "A2", "A3"])

        assert exc_info.value.status_code == 400
        assert "A1" in str(exc_info.value.message)
        assert "A2" in str(exc_info.value.message)
        # All seats should still exist
        assert sector.seats.count() == 3

    def test_bulk_delete_seats_allowed_with_non_blocking_tickets(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test bulk delete succeeds when tickets are cancelled or for past events."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat1 = VenueSeat.objects.create(sector=sector, label="A1")
        seat2 = VenueSeat.objects.create(sector=sector, label="A2")

        future_event = Event.objects.create(
            organization=organization,
            name="Future Concert",
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=7, hours=3),
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=future_event, name="General", price=50)
        # Cancelled ticket - should not block
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat1,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.CANCELLED,
        )
        # Checked-in ticket - should not block
        Ticket.objects.create(
            guest_name="Test Guest",
            event=future_event,
            user=organization_owner_user,
            tier=tier,
            seat=seat2,
            sector=sector,
            venue=venue,
            status=Ticket.TicketStatus.CHECKED_IN,
            checked_in_at=timezone.now(),
        )

        deleted_count = venue_service.bulk_delete_seats(sector, ["A1", "A2"])

        assert deleted_count == 2
        assert sector.seats.count() == 0


class TestBulkUpdateSeats:
    """Tests for bulk seat updates."""

    def test_bulk_update_seats_success(self, organization: Organization) -> None:
        """Test bulk updating seats successfully."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1", row="A", number=1, is_accessible=False)
        VenueSeat.objects.create(sector=sector, label="A2", row="A", number=2, is_accessible=False)
        VenueSeat.objects.create(sector=sector, label="A3", row="A", number=3, is_accessible=False)

        updates = [
            schema.VenueSeatBulkUpdateItemSchema(label="A1", is_accessible=True),  # type: ignore[call-arg]
            schema.VenueSeatBulkUpdateItemSchema(label="A2", row="B", number=1),  # type: ignore[call-arg]
        ]

        updated_seats = venue_service.bulk_update_seats(sector, updates)

        assert len(updated_seats) == 2

        # Verify changes in DB
        a1 = sector.seats.get(label="A1")
        assert a1.is_accessible is True

        a2 = sector.seats.get(label="A2")
        assert a2.row == "B"
        assert a2.number == 1

        # A3 should be unchanged
        a3 = sector.seats.get(label="A3")
        assert a3.is_accessible is False
        assert a3.row == "A"

    def test_bulk_update_seats_empty_list(self, organization: Organization) -> None:
        """Test bulk update with empty list returns empty."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")

        updated_seats = venue_service.bulk_update_seats(sector, [])

        assert updated_seats == []

    def test_bulk_update_seats_not_found(self, organization: Organization) -> None:
        """Test bulk update with non-existent seats raises error."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1", is_accessible=False)

        updates = [
            schema.VenueSeatBulkUpdateItemSchema(label="A1", is_accessible=True),  # type: ignore[call-arg]
            schema.VenueSeatBulkUpdateItemSchema(label="NONEXISTENT", is_accessible=True),  # type: ignore[call-arg]
        ]

        with pytest.raises(HttpError) as exc_info:
            venue_service.bulk_update_seats(sector, updates)

        assert exc_info.value.status_code == 404
        assert "NONEXISTENT" in str(exc_info.value.message)

        # A1 should still be unchanged - atomic rollback
        a1 = sector.seats.get(label="A1")
        assert a1.is_accessible is False

    def test_bulk_update_seats_validates_positions(self, organization: Organization) -> None:
        """Test that bulk update validates seat positions against sector shape."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )
        VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")

        updates = [
            schema.VenueSeatBulkUpdateItemSchema(label="A1", position=schema.Coordinate2D(x=50, y=50)),  # type: ignore[call-arg]  # Inside
            schema.VenueSeatBulkUpdateItemSchema(label="A2", position=schema.Coordinate2D(x=150, y=50)),  # type: ignore[call-arg]  # Outside
        ]

        with pytest.raises(HttpError) as exc_info:
            venue_service.bulk_update_seats(sector, updates)

        assert exc_info.value.status_code == 400
        assert "A2" in str(exc_info.value.message)
        assert "outside the sector shape" in str(exc_info.value.message)

    def test_bulk_update_seats_no_shape_skips_validation(self, organization: Organization) -> None:
        """Test that positions are not validated when sector has no shape."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Floor")  # No shape
        VenueSeat.objects.create(sector=sector, label="A1")

        updates = [
            schema.VenueSeatBulkUpdateItemSchema(label="A1", position=schema.Coordinate2D(x=9999, y=9999)),  # type: ignore[call-arg]
        ]

        updated_seats = venue_service.bulk_update_seats(sector, updates)

        assert len(updated_seats) == 1
        a1 = sector.seats.get(label="A1")
        assert a1.position == {"x": 9999, "y": 9999}

    def test_bulk_update_seats_no_changes(self, organization: Organization) -> None:
        """Test bulk update with no actual changes still returns seats."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1", row="A")

        # Update with only label (identifier), no changes
        updates = [
            schema.VenueSeatBulkUpdateItemSchema(label="A1"),  # type: ignore[call-arg]
        ]

        updated_seats = venue_service.bulk_update_seats(sector, updates)

        assert len(updated_seats) == 1
        a1 = sector.seats.get(label="A1")
        assert a1.row == "A"


class TestSectorMetadata:
    """Tests for sector metadata field."""

    def test_create_sector_with_metadata(self, organization: Organization) -> None:
        """Test creating a sector with metadata."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        metadata = {"aisle_positions": [{"x": 50, "y": 0}, {"x": 50, "y": 100}], "label_offset": 10}
        payload = schema.VenueSectorCreateSchema(name="Orchestra", metadata=metadata)  # type: ignore[call-arg]

        sector = venue_service.create_sector(venue, payload)

        assert sector.metadata == metadata
        sector.refresh_from_db()
        assert sector.metadata == metadata

    def test_create_sector_without_metadata(self, organization: Organization) -> None:
        """Test creating a sector without metadata defaults to None."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        payload = schema.VenueSectorCreateSchema(name="Orchestra")  # type: ignore[call-arg]

        sector = venue_service.create_sector(venue, payload)

        assert sector.metadata is None

    def test_update_sector_metadata(self, organization: Organization) -> None:
        """Test updating a sector's metadata."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra", metadata={"old": "data"})
        new_metadata = {"new": "data", "nested": {"key": "value"}}
        payload = schema.VenueSectorUpdateSchema(metadata=new_metadata)  # type: ignore[call-arg]

        updated = venue_service.update_sector(sector, payload)

        assert updated.metadata == new_metadata
        sector.refresh_from_db()
        assert sector.metadata == new_metadata

    def test_update_sector_clear_metadata(self, organization: Organization) -> None:
        """Test clearing sector metadata by setting to empty dict."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra", metadata={"old": "data"})
        payload = schema.VenueSectorUpdateSchema(metadata={})  # type: ignore[call-arg]

        updated = venue_service.update_sector(sector, payload)

        assert updated.metadata == {}
        sector.refresh_from_db()
        assert sector.metadata == {}
