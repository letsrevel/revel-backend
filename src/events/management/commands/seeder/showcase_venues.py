"""Showcase venue seeding module.

Creates three fully-laid-out, realistic venues with sectors, materialized seats,
ticket tiers exercising every seat assignment mode, and sold tickets referencing
seats. All generation is deterministic (fixed layouts, seeded random for seat
picks) so repeated runs with the same seed produce the same structures.

Venues:
    - Teatro Grande: large theatre (Platea, Galleria, 4 Palchi) with ~1,350 seats.
    - The Chuckle Cellar: comedy club (front tables, riser rows, standing room).
    - Mittelfest Halle: mid-size music venue (GA floor + seated balcony).
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from accounts.models import RevelUser
from events.management.commands.seeder.base import BaseSeeder
from events.models import (
    CancellationSource,
    Event,
    Organization,
    PriceCategory,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)

# Grid layouts: (row labels, seats per row, vertical aisle column indices).
# Positions follow the FE seat grid editor convention: x/y are grid indices,
# with x shifted by one for every aisle at a column index <= the seat column.
PLATEA_ROWS = [chr(c) for c in range(ord("A"), ord("V") + 1)]  # A-V, 22 rows
PLATEA_SEATS_PER_ROW = 34
PLATEA_AISLES = [17]  # Center aisle
PLATEA_PREMIUM_ROWS = PLATEA_ROWS[:8]  # A-H: premium price category

GALLERIA_ROWS = [chr(c) for c in range(ord("A"), ord("K") + 1)]  # A-K, 11 rows
GALLERIA_SEATS_PER_ROW = 52
GALLERIA_AISLES = [13, 39]  # Two aisles splitting the balcony in thirds

CHUCKLE_TABLE_ROWS = ["T1", "T2", "T3", "T4"]
CHUCKLE_TABLE_SEATS_PER_ROW = 8
CHUCKLE_RISER_ROWS = ["A", "B", "C", "D"]
CHUCKLE_RISER_SEATS_PER_ROW = 12

MITTELFEST_BALCONY_ROWS = ["A", "B", "C", "D", "E", "F"]
MITTELFEST_BALCONY_SEATS_PER_ROW = 20
MITTELFEST_BALCONY_AISLES = [10]  # Center aisle

# Seat occupancy for the first event of each venue (active tickets), as a
# fraction of the sector's active seats.
SEATED_OCCUPANCY = 0.20
PENDING_TICKETS_PER_SECTOR = 4
CANCELLED_TICKETS_PER_SECTOR = 3


class ShowcaseVenueSeeder(BaseSeeder):
    """Seeder for showcase venues with realistic seat maps and sold tickets."""

    def seed(self) -> None:
        """Seed showcase venues, events, tiers, and tickets."""
        if len(self.state.organizations) < 3:
            self.log("  Skipping showcase venues (need at least 3 organizations)")
            return

        self._user_idx = 0
        self._seed_teatro_grande(self.state.organizations[0])
        self._seed_chuckle_cellar(self.state.organizations[1])
        self._seed_mittelfest_halle(self.state.organizations[2])

    # -- Layout helpers ----------------------------------------------------

    def _aisle_metadata(self, vertical_aisles: list[int]) -> dict[str, t.Any]:
        """Build sector metadata matching the FE seat grid editor format."""
        return {
            "aisles": {
                "verticalAisles": vertical_aisles,
                "horizontalAisles": [],
                "invertRowOrder": False,
            }
        }

    def _grid_shape(self, num_rows: int, seats_per_row: int, num_aisles: int) -> list[list[int]]:
        """Build a rectangular polygon roughly bounding the seat grid."""
        width = seats_per_row + num_aisles
        return [[0, 0], [width, 0], [width, num_rows], [0, num_rows]]

    def _build_grid_seats(
        self,
        sector: VenueSector,
        rows: list[str],
        seats_per_row: int,
        vertical_aisles: list[int],
        accessible: set[str] | None = None,
        obstructed: set[str] | None = None,
        inactive: set[str] | None = None,
        label_separator: str = "",
        price_category: PriceCategory | None = None,
    ) -> list[VenueSeat]:
        """Build seat instances for a rectangular grid with aisle gaps.

        Rows are generated front-to-back and seats left-to-right, so row_order
        and adjacency_index are simply the generation indices.
        """
        accessible = accessible or set()
        obstructed = obstructed or set()
        inactive = inactive or set()

        seats: list[VenueSeat] = []
        for row_idx, row in enumerate(rows):
            for col in range(seats_per_row):
                x = col + sum(1 for aisle in vertical_aisles if aisle <= col)
                label = f"{row}{label_separator}{col + 1}"
                seats.append(
                    VenueSeat(
                        sector=sector,
                        label=label,
                        row_label=row,
                        number=col + 1,
                        row_order=row_idx,
                        adjacency_index=col,
                        default_price_category=price_category,
                        position={"x": x, "y": row_idx},
                        is_accessible=label in accessible,
                        is_obstructed_view=label in obstructed,
                        is_active=label not in inactive,
                    )
                )
        return seats

    def _row_end_labels(self, rows: list[str], seats_per_row: int, per_side: int = 2, separator: str = "") -> set[str]:
        """Labels of the first and last `per_side` seats of each given row."""
        labels: set[str] = set()
        for row in rows:
            for i in range(1, per_side + 1):
                labels.add(f"{row}{separator}{i}")
                labels.add(f"{row}{separator}{seats_per_row - i + 1}")
        return labels

    def _price_category(self, venue: Venue, name: str, color: str, display_order: int) -> PriceCategory:
        """Create a venue-scoped price category for painting onto seats."""
        return PriceCategory.objects.create(venue=venue, name=name, color=color, display_order=display_order)

    # -- Venue builders ----------------------------------------------------

    def _seed_teatro_grande(self, org: Organization) -> None:
        """Seed a large theatre: Platea, Galleria, and four Palchi."""
        self.log("Creating showcase venue: Teatro Grande...")

        venue = Venue.objects.create(
            organization=org,
            name="Teatro Grande",
            slug="teatro-grande",
            description="A grand historic theatre with stalls, gallery, and boxes.",
            capacity=1400,
            address="Piazza del Teatro 1",
        )

        platea = VenueSector.objects.create(
            venue=venue,
            name="Platea",
            code="PLT",
            display_order=0,
            shape=self._grid_shape(len(PLATEA_ROWS), PLATEA_SEATS_PER_ROW, len(PLATEA_AISLES)),
            metadata=self._aisle_metadata(PLATEA_AISLES),
        )
        galleria = VenueSector.objects.create(
            venue=venue,
            name="Galleria",
            code="GAL",
            display_order=1,
            shape=self._grid_shape(len(GALLERIA_ROWS), GALLERIA_SEATS_PER_ROW, len(GALLERIA_AISLES)),
            metadata=self._aisle_metadata(GALLERIA_AISLES),
        )
        palchi = [
            VenueSector.objects.create(
                venue=venue,
                name=f"Palco {i}",
                code=f"P{i}",
                display_order=1 + i,
                shape=[[0, 0], [4, 0], [4, 2], [0, 2]],
            )
            for i in range(1, 5)
        ]

        cat_platea_premium = self._price_category(venue, "Platea Premium", "#dc2626", 0)
        cat_platea = self._price_category(venue, "Platea", "#f59e0b", 1)
        cat_galleria = self._price_category(venue, "Galleria", "#7c3aed", 2)
        cat_palco = self._price_category(venue, "Palco", "#0ea5e9", 3)

        seats: list[VenueSeat] = []

        # Platea: accessible seats at the ends of the front and back rows,
        # a couple of decommissioned seats in the back row. Front rows are
        # painted with the premium category, the rest with the standard one.
        platea_seats = self._build_grid_seats(
            platea,
            PLATEA_ROWS,
            PLATEA_SEATS_PER_ROW,
            PLATEA_AISLES,
            accessible=self._row_end_labels(["A", "B", "U", "V"], PLATEA_SEATS_PER_ROW),
            inactive={"V17", "V18"},
            price_category=cat_platea,
        )
        for seat in platea_seats:
            if seat.row_label in PLATEA_PREMIUM_ROWS:
                seat.default_price_category = cat_platea_premium
        seats.extend(platea_seats)

        # Galleria: accessible ends on the front row, obstructed views at the
        # extreme ends of the rear rows, one broken seat.
        seats.extend(
            self._build_grid_seats(
                galleria,
                GALLERIA_ROWS,
                GALLERIA_SEATS_PER_ROW,
                GALLERIA_AISLES,
                accessible=self._row_end_labels(["A"], GALLERIA_SEATS_PER_ROW),
                obstructed=self._row_end_labels(["J", "K"], GALLERIA_SEATS_PER_ROW),
                inactive={"F26"},
                price_category=cat_galleria,
            )
        )

        # Palchi: two rows of four seats; the seats closest to the stage-side
        # partition have an obstructed view.
        for palco in palchi:
            seats.extend(
                self._build_grid_seats(
                    palco,
                    ["1", "2"],
                    4,
                    [],
                    obstructed={"1-4", "2-4"},
                    label_separator="-",
                    price_category=cat_palco,
                )
            )

        self.batch_create(VenueSeat, seats, desc="Creating Teatro Grande seats")
        self.log(f"  Teatro Grande: {len(seats)} seats in {2 + len(palchi)} sectors")

        events = self._create_showcase_events(
            venue,
            [
                ("La Traviata — Season Opening", "teatro-grande-traviata", 30),
                ("Symphony No. 9 — New Year Gala", "teatro-grande-gala", 60),
            ],
        )

        for event in events:
            tiers = [
                self._create_tier(event, "Platea", platea, TicketTier.SeatAssignmentMode.USER_CHOICE, Decimal("45")),
                self._create_tier(
                    event,
                    "Galleria",
                    galleria,
                    TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
                    Decimal("25"),
                    price_category=cat_galleria,
                ),
                self._create_tier(
                    event, "Palco 1", palchi[0], TicketTier.SeatAssignmentMode.USER_CHOICE, Decimal("80")
                ),
            ]
            if event is events[0]:
                for tier in tiers:
                    self._sell_seated_tickets(event, tier)

    def _seed_chuckle_cellar(self, org: Organization) -> None:
        """Seed a comedy club: front tables, riser rows, and standing room."""
        self.log("Creating showcase venue: The Chuckle Cellar...")

        venue = Venue.objects.create(
            organization=org,
            name="The Chuckle Cellar",
            slug="the-chuckle-cellar",
            description="An intimate basement comedy club with table seating and a standing bar area.",
            capacity=120,
            address="12 Basement Lane",
        )

        tables = VenueSector.objects.create(
            venue=venue,
            name="Front Tables",
            code="TBL",
            display_order=0,
            shape=self._grid_shape(len(CHUCKLE_TABLE_ROWS), CHUCKLE_TABLE_SEATS_PER_ROW, 0),
            metadata=self._aisle_metadata([]),
        )
        riser = VenueSector.objects.create(
            venue=venue,
            name="Riser",
            code="RSR",
            display_order=1,
            shape=self._grid_shape(len(CHUCKLE_RISER_ROWS), CHUCKLE_RISER_SEATS_PER_ROW, 0),
            metadata=self._aisle_metadata([]),
        )
        # GA works as a seatless standing sector with a hard capacity.
        standing = VenueSector.objects.create(
            venue=venue,
            name="Standing Room",
            code="STD",
            kind=VenueSector.Kind.STANDING,
            display_order=2,
            capacity=40,
            shape=[[0, 0], [12, 0], [12, 4], [0, 4]],
        )

        cat_tables = self._price_category(venue, "Front Tables", "#ea580c", 0)
        cat_riser = self._price_category(venue, "Riser", "#0d9488", 1)

        seats: list[VenueSeat] = []
        seats.extend(
            self._build_grid_seats(
                tables,
                CHUCKLE_TABLE_ROWS,
                CHUCKLE_TABLE_SEATS_PER_ROW,
                [],
                accessible={"T1-1", "T1-8"},
                label_separator="-",
                price_category=cat_tables,
            )
        )
        # Riser: a support pillar obstructs two seats in the back row.
        seats.extend(
            self._build_grid_seats(
                riser,
                CHUCKLE_RISER_ROWS,
                CHUCKLE_RISER_SEATS_PER_ROW,
                [],
                accessible={"D1", "D12"},
                obstructed={"D6", "D7"},
                inactive={"C12"},
                price_category=cat_riser,
            )
        )

        self.batch_create(VenueSeat, seats, desc="Creating Chuckle Cellar seats")
        self.log(f"  The Chuckle Cellar: {len(seats)} seats in 3 sectors (+{standing.capacity} standing)")

        events = self._create_showcase_events(
            venue,
            [
                ("Open Mic Friday", "chuckle-open-mic", 14),
                ("Headliner Night: Late Show", "chuckle-headliner", 45),
            ],
        )

        for event in events:
            tiers = [
                self._create_tier(
                    event, "Front Table", tables, TicketTier.SeatAssignmentMode.USER_CHOICE, Decimal("20")
                ),
                self._create_tier(
                    event,
                    "Riser",
                    riser,
                    TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
                    Decimal("15"),
                    price_category=cat_riser,
                ),
                self._create_tier(event, "Standing", standing, TicketTier.SeatAssignmentMode.NONE, Decimal("0")),
            ]
            if event is events[0]:
                self._sell_seated_tickets(event, tiers[0])
                self._sell_seated_tickets(event, tiers[1])
                self._sell_ga_tickets(event, tiers[2], num_tickets=12)

    def _seed_mittelfest_halle(self, org: Organization) -> None:
        """Seed a mid-size music venue: GA floor plus a seated balcony."""
        self.log("Creating showcase venue: Mittelfest Halle...")

        venue = Venue.objects.create(
            organization=org,
            name="Mittelfest Halle",
            slug="mittelfest-halle",
            description="A mid-size concert hall with a general admission floor and seated balcony.",
            capacity=720,
            address="Hallenstrasse 7",
        )

        floor = VenueSector.objects.create(
            venue=venue,
            name="Floor",
            code="FLR",
            kind=VenueSector.Kind.STANDING,
            display_order=0,
            capacity=600,
            shape=[[0, 0], [40, 0], [40, 25], [0, 25]],
        )
        balcony = VenueSector.objects.create(
            venue=venue,
            name="Balcony",
            code="BAL",
            display_order=1,
            shape=self._grid_shape(len(MITTELFEST_BALCONY_ROWS), MITTELFEST_BALCONY_SEATS_PER_ROW, 1),
            metadata=self._aisle_metadata(MITTELFEST_BALCONY_AISLES),
        )

        cat_balcony = self._price_category(venue, "Balcony", "#2563eb", 0)

        seats = self._build_grid_seats(
            balcony,
            MITTELFEST_BALCONY_ROWS,
            MITTELFEST_BALCONY_SEATS_PER_ROW,
            MITTELFEST_BALCONY_AISLES,
            accessible=self._row_end_labels(["A"], MITTELFEST_BALCONY_SEATS_PER_ROW),
            obstructed={"F1", "F20"},
            inactive={"E11"},
            price_category=cat_balcony,
        )

        self.batch_create(VenueSeat, seats, desc="Creating Mittelfest Halle seats")
        self.log(f"  Mittelfest Halle: {len(seats)} seats in 2 sectors (+{floor.capacity} GA floor)")

        events = self._create_showcase_events(
            venue,
            [
                ("Indie Rock Festival Warm-Up", "mittelfest-warmup", 21),
                ("Jazz & Wine Evening", "mittelfest-jazz", 75),
            ],
        )

        modes = TicketTier.SeatAssignmentMode
        for i, event in enumerate(events):
            balcony_mode = modes.USER_CHOICE if i == 0 else modes.BEST_AVAILABLE
            tiers = [
                self._create_tier(
                    event,
                    "Balcony Seated",
                    balcony,
                    balcony_mode,
                    Decimal("35"),
                    price_category=cat_balcony if balcony_mode == modes.BEST_AVAILABLE else None,
                ),
                self._create_tier(event, "GA Floor", floor, TicketTier.SeatAssignmentMode.NONE, Decimal("0")),
            ]
            if event is events[0]:
                self._sell_seated_tickets(event, tiers[0])
                self._sell_ga_tickets(event, tiers[1], num_tickets=80)

    # -- Event / tier / ticket helpers -------------------------------------

    def _create_showcase_events(self, venue: Venue, specs: list[tuple[str, str, int]]) -> list[Event]:
        """Create future, open, ticketed events at a venue.

        Args:
            venue: The venue to attach events to.
            specs: List of (name, slug, days_from_now) tuples.

        Returns:
            The created events.
        """
        now = timezone.now()
        events = [
            Event(
                organization=venue.organization,
                name=name,
                slug=slug,
                description=f"{name} at {venue.name}.",
                status=Event.EventStatus.OPEN,
                visibility=Event.Visibility.PUBLIC,
                event_type=Event.EventType.PUBLIC,
                start=now + timedelta(days=days),
                end=now + timedelta(days=days, hours=3),
                max_attendees=0,
                requires_ticket=True,
                venue=venue,
                address=venue.address,
            )
            for name, slug, days in specs
        ]
        return self.batch_create(Event, events, desc=f"Creating {venue.name} events")

    def _create_tier(
        self,
        event: Event,
        name: str,
        sector: VenueSector,
        mode: "TicketTier.SeatAssignmentMode",
        price: Decimal,
        price_category: PriceCategory | None = None,
    ) -> TicketTier:
        """Create a ticket tier bound to a venue sector.

        Free tiers use the FREE payment method; priced tiers use OFFLINE so no
        Stripe involvement is needed. Best-available tiers must pass the price
        category whose seat pool they draw from.
        """
        is_free = price == 0
        return TicketTier.objects.create(
            event=event,
            name=name,
            description=f"{name} ticket for {event.name}",
            visibility=TicketTier.Visibility.PUBLIC,
            payment_method=TicketTier.PaymentMethod.FREE if is_free else TicketTier.PaymentMethod.OFFLINE,
            price=price,
            manual_payment_instructions=None if is_free else "Please transfer to IBAN: XX1234567890",
            venue=sector.venue,
            sector=sector,
            price_category=price_category,
            seat_assignment_mode=mode,
        )

    def _next_user(self) -> RevelUser:
        """Cycle deterministically through seeded users."""
        user = self.state.users[self._user_idx % len(self.state.users)]
        self._user_idx += 1
        return user

    def _sell_seated_tickets(self, event: Event, tier: TicketTier) -> None:
        """Create tickets occupying seats in the tier's sector.

        Mirrors the purchase flow: ticket.venue/sector/seat match the tier, the
        (event, seat) pair is unique for non-cancelled tickets, and the tier's
        quantity_sold reflects non-cancelled tickets.
        """
        active_seats = list(
            VenueSeat.objects.filter(sector_id=tier.sector_id, is_active=True).order_by("row_order", "adjacency_index")
        )
        num_active = max(1, int(len(active_seats) * SEATED_OCCUPANCY))
        num_pending = min(PENDING_TICKETS_PER_SECTOR, len(active_seats) - num_active)
        num_cancelled = min(CANCELLED_TICKETS_PER_SECTOR, len(active_seats) - num_active - num_pending)

        picked = self.random_sample(active_seats, num_active + num_pending + num_cancelled)
        now = timezone.now()

        tickets: list[Ticket] = []
        for i, seat in enumerate(picked):
            if i < num_active:
                status = Ticket.TicketStatus.ACTIVE
            elif i < num_active + num_pending:
                # PENDING only makes sense for OFFLINE tiers awaiting payment.
                status = (
                    Ticket.TicketStatus.PENDING
                    if tier.payment_method == TicketTier.PaymentMethod.OFFLINE
                    else Ticket.TicketStatus.ACTIVE
                )
            else:
                status = Ticket.TicketStatus.CANCELLED

            user = self._next_user()
            ticket = Ticket(
                event=event,
                user=user,
                tier=tier,
                status=status,
                guest_name=user.get_display_name(),
                venue=tier.venue,
                sector=tier.sector,
                seat=seat,
            )
            if status == Ticket.TicketStatus.CANCELLED:
                ticket.cancelled_at = now
                ticket.cancellation_source = CancellationSource.USER
            tickets.append(ticket)

        self.batch_create(Ticket, tickets, desc=f"Creating {tier.name} tickets")
        tier.quantity_sold = num_active + num_pending
        tier.save(update_fields=["quantity_sold"])
        self.log(f"  {event.name} / {tier.name}: {num_active} active, {num_pending} pending, {num_cancelled} cancelled")

    def _sell_ga_tickets(self, event: Event, tier: TicketTier, num_tickets: int) -> None:
        """Create seatless GA tickets on a capacity-only sector tier."""
        tickets: list[Ticket] = []
        for _ in range(num_tickets):
            user = self._next_user()
            tickets.append(
                Ticket(
                    event=event,
                    user=user,
                    tier=tier,
                    status=Ticket.TicketStatus.ACTIVE,
                    guest_name=user.get_display_name(),
                    venue=tier.venue,
                    sector=tier.sector,
                )
            )

        self.batch_create(Ticket, tickets, desc=f"Creating {tier.name} tickets")
        tier.quantity_sold = num_tickets
        tier.save(update_fields=["quantity_sold"])
        self.log(f"  {event.name} / {tier.name}: {num_tickets} active GA tickets")
