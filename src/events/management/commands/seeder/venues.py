"""Venue seeding module."""

from events.management.commands.seeder.base import BaseSeeder
from events.models import Venue, VenueSeat, VenueSector

# Venue name templates
VENUE_NAMES = [
    "Main Hall",
    "Conference Center",
    "Exhibition Space",
    "Community Center",
    "Event Hall",
    "Auditorium",
    "Theater",
    "Ballroom",
    "Garden Pavilion",
    "Rooftop Terrace",
]

# Sector name templates
SECTOR_NAMES = [
    "Main Floor",
    "Balcony",
    "VIP Section",
    "Mezzanine",
    "Standing Area",
    "Orchestra",
    "Gallery",
    "Front Row",
    "General Admission",
    "Premium Seating",
]


class VenueSeeder(BaseSeeder):
    """Seeder for venues, sectors, and seats."""

    def seed(self) -> None:
        """Seed venues and related entities."""
        self._create_venues()
        self._create_sectors()
        self._create_seats()

    def _create_venues(self) -> None:
        """Create 1-3 venues per organization."""
        self.log("Creating venues...")

        venues_to_create: list[Venue] = []
        min_venues, max_venues = self.config.venues_per_org

        for org in self.state.organizations:
            num_venues = self.random_int(min_venues, max_venues)
            venue_names = self.random_sample(VENUE_NAMES, num_venues)

            org_venues: list[Venue] = []
            for i, name in enumerate(venue_names):
                venue = Venue(
                    organization=org,
                    name=f"{name} {i}",
                    slug=f"venue-{i}",
                    description=self.faker.paragraph() if self.random_bool(0.7) else "",
                    capacity=self.random_choice([None, 50, 100, 200, 500, 1000]) if self.random_bool(0.8) else None,
                    address=self.faker.address() if self.random_bool(0.8) else None,
                )
                venues_to_create.append(venue)
                org_venues.append(venue)

            self.state.venues[org.id] = org_venues

        created = self.batch_create(Venue, venues_to_create, desc="Creating venues")

        # Update state with actual created venues (with IDs)
        idx = 0
        for org in self.state.organizations:
            num_venues = len(self.state.venues.get(org.id, []))
            self.state.venues[org.id] = created[idx : idx + num_venues]
            idx += num_venues

        self.log(f"  Created {len(created)} venues")

    def _create_sectors(self) -> None:
        """Create 2-5 sectors per venue."""
        self.log("Creating venue sectors...")

        sectors_to_create: list[VenueSector] = []
        min_sectors, max_sectors = self.config.sectors_per_venue

        for org_venues in self.state.venues.values():
            for venue in org_venues:
                num_sectors = self.random_int(min_sectors, max_sectors)
                sector_names = self.random_sample(SECTOR_NAMES, num_sectors)

                venue_sectors: list[VenueSector] = []
                for i, name in enumerate(sector_names):
                    # Determine if this is a seated or GA section
                    is_seated = self.random_bool(0.6)

                    sector = VenueSector(
                        venue=venue,
                        name=name,
                        code=name[:3].upper() if self.random_bool(0.5) else None,
                        capacity=self.random_choice([20, 50, 100, 150]) if not is_seated else None,
                        display_order=i,
                        # Simple rectangular shape for testing
                        shape=[[0, 0], [100, 0], [100, 100], [0, 100]] if self.random_bool(0.3) else None,
                        metadata={"aisle": "left"} if self.random_bool(0.2) else None,
                    )
                    sectors_to_create.append(sector)
                    venue_sectors.append(sector)

                self.state.venue_sectors[venue.id] = venue_sectors

        created = self.batch_create(VenueSector, sectors_to_create, desc="Creating venue sectors")

        # Update state with actual created sectors (with IDs)
        idx = 0
        for org_venues in self.state.venues.values():
            for venue in org_venues:
                num_sectors = len(self.state.venue_sectors.get(venue.id, []))
                self.state.venue_sectors[venue.id] = created[idx : idx + num_sectors]
                idx += num_sectors

        self.log(f"  Created {len(created)} venue sectors")

    def _generate_sector_seats(self, sector: VenueSector) -> list[VenueSeat]:
        """Generate seats for a single sector."""
        rows = ["A", "B", "C", "D", "E", "F", "G", "H", "J", "K"]
        seats: list[VenueSeat] = []

        num_seats = self.random_int(10, 50)
        seats_per_row = (num_seats // len(rows)) + 1

        seat_count = 0
        for row in rows:
            if seat_count >= num_seats:
                break

            for num in range(1, seats_per_row + 1):
                if seat_count >= num_seats:
                    break

                seat = VenueSeat(
                    sector=sector,
                    label=f"{row}{num}",
                    row=row,
                    number=num,
                    position={"x": (num - 1) * 30, "y": rows.index(row) * 30} if self.random_bool(0.8) else None,
                    is_accessible=self.random_bool(0.05),
                    is_obstructed_view=self.random_bool(0.1),
                    is_active=self.random_bool(0.98),
                )
                seats.append(seat)
                seat_count += 1

        return seats

    def _create_seats(self) -> None:
        """Create seats for seated sectors (those without capacity)."""
        self.log("Creating venue seats...")

        seats_to_create: list[VenueSeat] = []

        for org_venues in self.state.venues.values():
            for venue in org_venues:
                sectors = self.state.venue_sectors.get(venue.id, [])

                for sector in sectors:
                    # Seated sectors have no capacity (capacity is determined by seat count)
                    if sector.capacity is not None:
                        continue

                    seats_to_create.extend(self._generate_sector_seats(sector))

        self.batch_create(VenueSeat, seats_to_create, desc="Creating venue seats")
        self.log(f"  Created {len(seats_to_create)} venue seats")
