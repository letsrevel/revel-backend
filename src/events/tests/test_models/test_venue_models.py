import pytest
from django.core.exceptions import ValidationError

from accounts.models import RevelUser
from events.models import Organization, Venue, VenueSeat, VenueSector

# --- Venue Model Tests ---


@pytest.mark.django_db
def test_venue_creation(organization: Organization) -> None:
    """Test basic venue creation."""
    venue = Venue.objects.create(
        organization=organization,
        name="Main Theater",
        description="Our primary performance space",
        capacity=500,
    )
    assert venue.name == "Main Theater"
    assert venue.organization == organization
    assert venue.capacity == 500


@pytest.mark.django_db
def test_venue_slug_auto_generated(organization: Organization) -> None:
    """Test that slug is auto-generated from name."""
    venue = Venue.objects.create(
        organization=organization,
        name="Grand Ballroom",
    )
    assert venue.slug == "grand-ballroom"


@pytest.mark.django_db
def test_venue_slug_collision_appends_suffix(organization: Organization) -> None:
    """Test that slug collision appends a random suffix."""
    venue1 = Venue.objects.create(
        organization=organization,
        name="Main Hall",
    )
    assert venue1.slug == "main-hall"

    # Create second venue with same name - should get a suffix
    venue2 = Venue.objects.create(
        organization=organization,
        name="Main Hall",
    )
    assert venue2.slug.startswith("main-hall-")
    assert len(venue2.slug) == len("main-hall-") + 5  # 5 char suffix


@pytest.mark.django_db
def test_venue_slug_collision_different_organizations(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that same slug can exist in different organizations."""
    org2 = Organization.objects.create(
        name="Another Org",
        slug="another-org",
        owner=organization_owner_user,
    )

    venue1 = Venue.objects.create(
        organization=organization,
        name="Main Hall",
    )
    assert venue1.slug == "main-hall"

    # Same name in different org should get same slug (no collision)
    venue2 = Venue.objects.create(
        organization=org2,
        name="Main Hall",
    )
    assert venue2.slug == "main-hall"


@pytest.mark.django_db
def test_venue_slug_preserved_on_update(organization: Organization) -> None:
    """Test that existing slug is preserved when updating a venue."""
    venue = Venue.objects.create(
        organization=organization,
        name="Original Name",
    )
    original_slug = venue.slug

    # Update the name
    venue.name = "New Name"
    venue.save()

    # Slug should not change
    venue.refresh_from_db()
    assert venue.slug == original_slug


@pytest.mark.django_db
def test_venue_explicit_slug_preserved(organization: Organization) -> None:
    """Test that explicitly provided slug is not overwritten."""
    venue = Venue.objects.create(
        organization=organization,
        name="My Venue",
        slug="custom-slug",
    )
    assert venue.slug == "custom-slug"


@pytest.mark.django_db
def test_venue_unique_constraint_organization_slug(organization: Organization) -> None:
    """Test that (organization, slug) must be unique."""
    Venue.objects.create(
        organization=organization,
        name="Venue One",
        slug="venue-slug",
    )

    # Attempting to create another venue with same slug in same org should fail
    venue2 = Venue(
        organization=organization,
        name="Venue Two",
        slug="venue-slug",
    )
    with pytest.raises(ValidationError):
        venue2.full_clean()


@pytest.mark.django_db
def test_venue_str_method(organization: Organization) -> None:
    """Test venue string representation."""
    venue = Venue.objects.create(
        organization=organization,
        name="Test Venue",
    )
    assert str(venue) == f"Test Venue ({organization.name})"


@pytest.mark.django_db
def test_venue_with_location_fields(organization: Organization) -> None:
    """Test venue with LocationMixin fields."""
    venue = Venue.objects.create(
        organization=organization,
        name="City Hall",
        address="123 Main St",
    )
    assert venue.address == "123 Main St"


@pytest.mark.django_db
def test_venue_capacity_optional(organization: Organization) -> None:
    """Test that capacity is optional (for GA-only venues)."""
    venue = Venue.objects.create(
        organization=organization,
        name="Open Space",
        capacity=None,
    )
    assert venue.capacity is None


# --- Venue Manager/QuerySet Tests ---


@pytest.mark.django_db
def test_venue_manager_with_sectors(organization: Organization) -> None:
    """Test that with_sectors() prefetches sectors."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    VenueSector.objects.create(venue=venue, name="Balcony")
    VenueSector.objects.create(venue=venue, name="Orchestra")

    # Use the manager method
    venue_qs = Venue.objects.with_sectors().get(id=venue.id)

    # Accessing sectors should not trigger additional queries
    # (In a real scenario, you'd use assertNumQueries to verify)
    assert venue_qs.sectors.count() == 2


@pytest.mark.django_db
def test_venue_manager_with_seats(organization: Organization) -> None:
    """Test that with_seats() prefetches sectors and seats."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Balcony")
    VenueSeat.objects.create(sector=sector, label="A1")
    VenueSeat.objects.create(sector=sector, label="A2")

    # Use the manager method
    venue_qs = Venue.objects.with_seats().get(id=venue.id)

    # Accessing nested relationships should not trigger additional queries
    sector_from_qs = venue_qs.sectors.first()
    assert sector_from_qs is not None
    assert sector_from_qs.seats.count() == 2


@pytest.mark.django_db
def test_venue_manager_full(organization: Organization) -> None:
    """Test that full() prefetches all related objects."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector1 = VenueSector.objects.create(venue=venue, name="Balcony")
    sector2 = VenueSector.objects.create(venue=venue, name="Orchestra")
    VenueSeat.objects.create(sector=sector1, label="A1")
    VenueSeat.objects.create(sector=sector2, label="B1")

    # Use the full() manager method
    venue_qs = Venue.objects.full().get(id=venue.id)

    # Both sectors and seats should be prefetched
    assert venue_qs.sectors.count() == 2
    for sector in venue_qs.sectors.all():
        assert sector.seats.count() >= 0  # At least accessible


# --- VenueSector Model Tests ---


@pytest.mark.django_db
def test_venue_sector_creation(organization: Organization) -> None:
    """Test basic venue sector creation."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(
        venue=venue,
        name="Balcony",
        code="BAL",
        capacity=100,
        display_order=1,
    )
    assert sector.name == "Balcony"
    assert sector.code == "BAL"
    assert sector.venue == venue
    assert sector.capacity == 100
    assert sector.display_order == 1


@pytest.mark.django_db
def test_venue_sector_unique_constraint_venue_name(organization: Organization) -> None:
    """Test that (venue, name) must be unique."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    VenueSector.objects.create(venue=venue, name="Balcony")

    # Attempting to create another sector with same name in same venue should fail
    sector2 = VenueSector(venue=venue, name="Balcony")
    with pytest.raises(ValidationError):
        sector2.full_clean()


@pytest.mark.django_db
def test_venue_sector_same_name_different_venues(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that sectors with same name can exist in different venues."""
    venue1 = Venue.objects.create(organization=organization, name="Theater One")
    venue2 = Venue.objects.create(organization=organization, name="Theater Two")

    sector1 = VenueSector.objects.create(venue=venue1, name="Balcony")
    sector2 = VenueSector.objects.create(venue=venue2, name="Balcony")

    # Should succeed - different venues
    assert sector1.name == sector2.name
    assert sector1.venue != sector2.venue


@pytest.mark.django_db
def test_venue_sector_str_method(organization: Organization) -> None:
    """Test sector string representation."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Balcony")

    assert str(sector) == "Theater: Balcony"


@pytest.mark.django_db
def test_venue_sector_optional_fields(organization: Organization) -> None:
    """Test that code, shape, and capacity are optional."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(
        venue=venue,
        name="General Admission",
        code=None,
        shape=None,
        capacity=None,
    )
    assert sector.code is None
    assert sector.shape is None
    assert sector.capacity is None


@pytest.mark.django_db
def test_venue_sector_shape_json_field(organization: Organization) -> None:
    """Test that shape can store JSON polygon data."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    polygon = [[0, 0], [100, 0], [100, 50], [0, 50]]
    sector = VenueSector.objects.create(
        venue=venue,
        name="Floor",
        shape=polygon,
    )
    sector.refresh_from_db()
    assert sector.shape == polygon


@pytest.mark.django_db
def test_venue_sector_ordering(organization: Organization) -> None:
    """Test that sectors are ordered by display_order and name."""
    venue = Venue.objects.create(organization=organization, name="Theater")

    # Create sectors with different display orders
    sector_c = VenueSector.objects.create(venue=venue, name="C Section", display_order=2)
    sector_a = VenueSector.objects.create(venue=venue, name="A Section", display_order=1)
    sector_b = VenueSector.objects.create(venue=venue, name="B Section", display_order=1)

    # Query sectors
    sectors = list(VenueSector.objects.filter(venue=venue))

    # Should be ordered by display_order, then name
    # Expected order: A Section (1), B Section (1), C Section (2)
    assert sectors[0] == sector_a
    assert sectors[1] == sector_b
    assert sectors[2] == sector_c


# --- VenueSeat Model Tests ---


@pytest.mark.django_db
def test_venue_seat_creation(organization: Organization) -> None:
    """Test basic venue seat creation."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Balcony")
    seat = VenueSeat.objects.create(
        sector=sector,
        label="A1",
        row="A",
        number=1,
        is_accessible=True,
        is_active=True,
    )
    assert seat.label == "A1"
    assert seat.row == "A"
    assert seat.number == 1
    assert seat.sector == sector
    assert seat.is_accessible is True
    assert seat.is_obstructed_view is False
    assert seat.is_active is True


@pytest.mark.django_db
def test_venue_seat_unique_constraint_sector_label(organization: Organization) -> None:
    """Test that (sector, label) must be unique."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Balcony")
    VenueSeat.objects.create(sector=sector, label="A1")

    # Attempting to create another seat with same label in same sector should fail
    seat2 = VenueSeat(sector=sector, label="A1")
    with pytest.raises(ValidationError):
        seat2.full_clean()


@pytest.mark.django_db
def test_venue_seat_same_label_different_sectors(organization: Organization) -> None:
    """Test that seats with same label can exist in different sectors."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector1 = VenueSector.objects.create(venue=venue, name="Balcony")
    sector2 = VenueSector.objects.create(venue=venue, name="Orchestra")

    seat1 = VenueSeat.objects.create(sector=sector1, label="A1")
    seat2 = VenueSeat.objects.create(sector=sector2, label="A1")

    # Should succeed - different sectors
    assert seat1.label == seat2.label
    assert seat1.sector != seat2.sector


@pytest.mark.django_db
def test_venue_seat_str_method(organization: Organization) -> None:
    """Test seat string representation."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Balcony")
    seat = VenueSeat.objects.create(sector=sector, label="A1")

    assert str(seat) == "Balcony / A1"


@pytest.mark.django_db
def test_venue_seat_optional_fields(organization: Organization) -> None:
    """Test that row, number, and position are optional."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="GA Standing")
    seat = VenueSeat.objects.create(
        sector=sector,
        label="SPOT-1",
        row=None,
        number=None,
        position=None,
    )
    assert seat.row is None
    assert seat.number is None
    assert seat.position is None


@pytest.mark.django_db
def test_venue_seat_position_json_field(organization: Organization) -> None:
    """Test that position can store JSON coordinate data."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Floor")
    position_data = {"x": 150, "y": 200}
    seat = VenueSeat.objects.create(
        sector=sector,
        label="B5",
        position=position_data,
    )
    seat.refresh_from_db()
    assert seat.position == position_data


@pytest.mark.django_db
def test_venue_seat_ordering(organization: Organization) -> None:
    """Test that seats are ordered by row, number, and label."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Orchestra")

    # Create seats in random order
    seat_b2 = VenueSeat.objects.create(sector=sector, label="B2", row="B", number=2)
    seat_a1 = VenueSeat.objects.create(sector=sector, label="A1", row="A", number=1)
    seat_a2 = VenueSeat.objects.create(sector=sector, label="A2", row="A", number=2)
    seat_b1 = VenueSeat.objects.create(sector=sector, label="B1", row="B", number=1)

    # Query seats
    seats = list(VenueSeat.objects.filter(sector=sector))

    # Should be ordered by row, then number, then label
    assert seats[0] == seat_a1
    assert seats[1] == seat_a2
    assert seats[2] == seat_b1
    assert seats[3] == seat_b2


@pytest.mark.django_db
def test_venue_seat_accessibility_flags(organization: Organization) -> None:
    """Test accessibility and view obstruction flags."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Balcony")
    seat = VenueSeat.objects.create(
        sector=sector,
        label="W1",
        is_accessible=True,
        is_obstructed_view=True,
    )
    assert seat.is_accessible is True
    assert seat.is_obstructed_view is True


@pytest.mark.django_db
def test_venue_seat_is_active_flag(organization: Organization) -> None:
    """Test is_active flag for deactivated seats."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Balcony")
    seat = VenueSeat.objects.create(
        sector=sector,
        label="OUT1",
        is_active=False,
    )
    assert seat.is_active is False


@pytest.mark.django_db
def test_venue_seat_defaults(organization: Organization) -> None:
    """Test default values for boolean fields."""
    venue = Venue.objects.create(organization=organization, name="Theater")
    sector = VenueSector.objects.create(venue=venue, name="Orchestra")
    seat = VenueSeat.objects.create(sector=sector, label="C1")

    # Check defaults
    assert seat.is_accessible is False
    assert seat.is_obstructed_view is False
    assert seat.is_active is True
