"""Tests for venue seat management endpoints."""

from datetime import timedelta

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationStaff, Ticket, TicketTier, Venue, VenueSeat, VenueSector

pytestmark = pytest.mark.django_db


class TestVenueSeatManagement:
    """Tests for individual seat update/delete endpoints."""

    def test_update_seat_by_label(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test updating a seat by its label."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1", is_accessible=False)

        url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        payload = {"is_accessible": True, "is_obstructed_view": True}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["is_accessible"] is True
        assert data["is_obstructed_view"] is True

        seat.refresh_from_db()
        assert seat.is_accessible is True
        assert seat.is_obstructed_view is True

    def test_update_seat_position_within_shape(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test updating seat position when sector has shape - valid position."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        payload = {"position": {"x": 50, "y": 50}}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["position"] == {"x": 50, "y": 50}

    def test_update_seat_position_outside_shape_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test updating seat position outside sector shape fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        payload = {"position": {"x": 150, "y": 50}}  # Outside

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "outside the sector shape" in response.json()["detail"]

    def test_update_seat_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test updating a non-existent seat returns 404."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "NONEXISTENT"},
        )
        payload = {"is_accessible": True}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 404

    def test_bulk_create_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk creating seats in a sector."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:bulk_create_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "row": "A", "number": 1},
                {"label": "A2", "row": "A", "number": 2},
                {"label": "A3", "row": "A", "number": 3, "is_accessible": True},
            ]
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 201
        data = response.json()
        assert len(data) == 3
        assert sector.seats.count() == 3
        assert sector.seats.filter(is_accessible=True).count() == 1

    def test_bulk_create_seats_with_shape_validation(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that bulk create validates seat positions against sector shape."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )

        url = reverse(
            "api:bulk_create_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "position": {"x": 50, "y": 50}},  # Inside
                {"label": "A2", "position": {"x": 150, "y": 50}},  # Outside!
            ]
        }

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "outside the sector shape" in response.json()["detail"]
        # No seats should have been created
        assert sector.seats.count() == 0

    def test_bulk_create_seats_empty_list_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that bulk create with empty list fails validation."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:bulk_create_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload: dict[str, list[dict[str, str]]] = {"seats": []}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422  # Pydantic validation error (min_length=1)

    def test_delete_seat_by_label(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test deleting a seat by its label."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSeat.objects.filter(id=seat.id).exists()

    def test_delete_seat_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test deleting a non-existent seat returns 404."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "NONEXISTENT"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 404

    def test_delete_seat_blocked_by_active_future_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with active ticket for future event cannot be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        # Create a future event with a ticket assigned to this seat
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

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 400
        assert "active or pending tickets" in response.json()["detail"]
        # Seat should still exist
        assert VenueSeat.objects.filter(id=seat.id).exists()

    def test_delete_seat_blocked_by_pending_future_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with pending ticket for future event cannot be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
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

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 400
        assert "active or pending tickets" in response.json()["detail"]

    def test_delete_seat_allowed_with_cancelled_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with cancelled ticket can be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
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
            status=Ticket.TicketStatus.CANCELLED,
        )

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSeat.objects.filter(id=seat.id).exists()

    def test_delete_seat_allowed_with_past_event_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with ticket for past event can be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        past_event = Event.objects.create(
            organization=organization,
            name="Past Concert",
            start=timezone.now() - timedelta(days=7),
            end=timezone.now() - timedelta(days=7) + timedelta(hours=3),  # 3 hours after start, still in the past
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

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSeat.objects.filter(id=seat.id).exists()

    def test_delete_seat_allowed_with_checked_in_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that seat with checked_in ticket for future event can be deleted."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")

        # Event starting soon (within next hour) but still future
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

        url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_owner_client.delete(url)

        assert response.status_code == 204
        assert not VenueSeat.objects.filter(id=seat.id).exists()

    def test_seat_operations_by_staff_without_permission(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that staff without edit_organization permission cannot modify seats."""
        perms = staff_member.permissions
        perms["default"]["edit_organization"] = False
        staff_member.permissions = perms
        staff_member.save()

        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")

        # Try update
        update_url = reverse(
            "api:update_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_staff_client.put(
            update_url, data=orjson.dumps({"is_accessible": True}), content_type="application/json"
        )
        assert response.status_code == 403

        # Try delete
        delete_url = reverse(
            "api:delete_venue_seat",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id, "label": "A1"},
        )
        response = organization_staff_client.delete(delete_url)
        assert response.status_code == 403

    def test_bulk_delete_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk deleting seats via API."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")
        VenueSeat.objects.create(sector=sector, label="A3")

        url = reverse(
            "api:bulk_delete_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"labels": ["A1", "A2"]}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["deleted"] == 2
        assert sector.seats.count() == 1
        assert sector.seats.filter(label="A3").exists()

    def test_bulk_delete_seats_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk delete with non-existent seats fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1")

        url = reverse(
            "api:bulk_delete_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"labels": ["A1", "NONEXISTENT"]}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 404
        assert "NONEXISTENT" in response.json()["detail"]
        # A1 should still exist (atomic rollback)
        assert sector.seats.filter(label="A1").exists()

    def test_bulk_delete_seats_blocked_by_ticket(
        self, organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test bulk delete blocked when seat has active ticket for future event."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")

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

        url = reverse(
            "api:bulk_delete_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {"labels": ["A1", "A2"]}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "A1" in response.json()["detail"]
        # Both seats should still exist (atomic rollback)
        assert sector.seats.count() == 2

    def test_bulk_delete_seats_empty_list_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test bulk delete with empty list fails validation."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:bulk_delete_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload: dict[str, list[str]] = {"labels": []}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422  # Pydantic validation error (min_length=1)

    def test_bulk_update_seats(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk updating seats via API."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1", row="A", number=1, is_accessible=False)
        VenueSeat.objects.create(sector=sector, label="A2", row="A", number=2, is_accessible=False)
        VenueSeat.objects.create(sector=sector, label="A3", row="A", number=3, is_accessible=False)

        url = reverse(
            "api:bulk_update_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "is_accessible": True},
                {"label": "A2", "row": "B", "number": 1},
            ]
        }

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

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

    def test_bulk_update_seats_not_found(self, organization_owner_client: Client, organization: Organization) -> None:
        """Test bulk update with non-existent seats fails."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")
        VenueSeat.objects.create(sector=sector, label="A1", is_accessible=False)

        url = reverse(
            "api:bulk_update_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "is_accessible": True},
                {"label": "NONEXISTENT", "is_accessible": True},
            ]
        }

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 404
        assert "NONEXISTENT" in response.json()["detail"]

        # A1 should still be unchanged (atomic rollback)
        a1 = sector.seats.get(label="A1")
        assert a1.is_accessible is False

    def test_bulk_update_seats_position_validation(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test bulk update validates position against sector shape."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            shape=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}],
        )
        VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")

        url = reverse(
            "api:bulk_update_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload = {
            "seats": [
                {"label": "A1", "position": {"x": 50, "y": 50}},  # Inside
                {"label": "A2", "position": {"x": 150, "y": 50}},  # Outside
            ]
        }

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "A2" in response.json()["detail"]
        assert "outside" in response.json()["detail"]

    def test_bulk_update_seats_empty_list_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test bulk update with empty list fails validation."""
        venue = Venue.objects.create(organization=organization, name="Theater")
        sector = VenueSector.objects.create(venue=venue, name="Orchestra")

        url = reverse(
            "api:bulk_update_venue_seats",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        payload: dict[str, list[dict[str, str]]] = {"seats": []}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 422  # Pydantic validation error (min_length=1)
