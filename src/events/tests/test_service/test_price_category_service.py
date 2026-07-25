"""Tests for the venue price category service layer."""

import pytest
from django.core.exceptions import ValidationError
from ninja.errors import HttpError

from events import schema
from events.models import Event, Organization, PriceCategory, TicketTier, Venue, VenueSeat, VenueSector
from events.service import venue_service

pytestmark = pytest.mark.django_db


class TestCreatePriceCategory:
    """Tests for price category creation."""

    def test_create_price_category(self, organization: Organization) -> None:
        """Test creating a price category with defaults."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        payload = schema.PriceCategoryCreateSchema(name="Premium", color="#aa0000")  # type: ignore[call-arg]

        category = venue_service.create_price_category(venue, payload)

        assert category.name == "Premium"
        assert category.color == "#aa0000"
        assert category.display_order == 0
        assert category.venue == venue

    def test_create_price_category_with_display_order(self, organization: Organization) -> None:
        """Test creating a price category with an explicit display order."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        payload = schema.PriceCategoryCreateSchema(name="Standard", color="#0000aa", display_order=3)

        category = venue_service.create_price_category(venue, payload)

        assert category.display_order == 3

    def test_create_price_category_duplicate_name_rejected(self, organization: Organization) -> None:
        """Duplicate (venue, name) surfaces as a Django ValidationError (400-able)."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")

        payload = schema.PriceCategoryCreateSchema(name="Premium", color="#bb0000")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            venue_service.create_price_category(venue, payload)


class TestUpdatePriceCategory:
    """Tests for price category updates."""

    def test_update_price_category_fields(self, organization: Organization) -> None:
        """Test updating a price category's name, color, and order."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        category = PriceCategory.objects.create(venue=venue, name="Old", color="#aa0000")

        payload = schema.PriceCategoryUpdateSchema(name="New", color="#00aa00", display_order=5)
        updated = venue_service.update_price_category(category, payload)

        assert updated.name == "New"
        assert updated.color == "#00aa00"
        assert updated.display_order == 5
        category.refresh_from_db()
        assert category.name == "New"

    def test_update_price_category_no_changes(self, organization: Organization) -> None:
        """Test update with no fields returns the category unchanged."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        category = PriceCategory.objects.create(venue=venue, name="Keep", color="#aa0000")

        payload = schema.PriceCategoryUpdateSchema()  # type: ignore[call-arg]
        updated = venue_service.update_price_category(category, payload)

        assert updated.name == "Keep"


class TestDeletePriceCategory:
    """Tests for price category deletion and its tier guard."""

    def test_delete_price_category_success(self, organization: Organization) -> None:
        """Test deleting an unreferenced category succeeds and unpaints seats."""
        venue = Venue.objects.create(organization=organization, name="Venue")
        sector = VenueSector.objects.create(venue=venue, name="Stalls")
        category = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)

        venue_service.delete_price_category(category)

        assert not PriceCategory.objects.filter(id=category.id).exists()
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    @pytest.mark.parametrize(
        "mode",
        [TicketTier.SeatAssignmentMode.BEST_AVAILABLE, TicketTier.SeatAssignmentMode.USER_CHOICE],
    )
    def test_delete_price_category_blocked_by_tier(
        self, organization: Organization, event: Event, mode: TicketTier.SeatAssignmentMode
    ) -> None:
        """Deleting a category priced by a ticket tier is refused with 400, in either seated mode.

        Since v3 the map is the sole pricing mechanism, so a best-available tier references
        the category exactly like a user-choice one and must block the delete identically.
        """
        venue = Venue.objects.create(organization=organization, name="Venue")
        event.venue = venue
        event.save(update_fields=["venue"])
        sector = VenueSector.objects.create(venue=venue, name="Stalls")
        category = PriceCategory.objects.create(venue=venue, name="Gold", color="#ffaa00")
        VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        TicketTier.objects.create(
            event=event,
            name="Gold",
            sector=sector,
            seat_assignment_mode=mode,
            category_prices={str(category.id): "10.00"},
        )

        with pytest.raises(HttpError) as exc_info:
            venue_service.delete_price_category(category)

        assert exc_info.value.status_code == 400
        assert "ticket tiers" in str(exc_info.value)
        # The FK that "reassigning a tier" once meant is gone: the map is the only fix.
        assert "category prices" in str(exc_info.value)
        assert PriceCategory.objects.filter(id=category.id).exists()
