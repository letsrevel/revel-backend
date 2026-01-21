"""Ticket seeding module."""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from events.management.commands.seeder.base import BaseSeeder
from events.models import Payment, Ticket, TicketTier

# Ticket tier name templates
TIER_NAMES = [
    "General Admission",
    "VIP",
    "Early Bird",
    "Student",
    "Group",
    "Premium",
    "Standard",
    "Late Bird",
    "Member Special",
    "Sponsor",
]

# Ticket status map (module-level constant to reduce complexity)
TICKET_STATUS_MAP = {
    "active": Ticket.TicketStatus.ACTIVE,
    "pending": Ticket.TicketStatus.PENDING,
    "checked_in": Ticket.TicketStatus.CHECKED_IN,
    "cancelled": Ticket.TicketStatus.CANCELLED,
}


class TicketSeeder(BaseSeeder):
    """Seeder for ticket tiers, tickets, and payments."""

    def seed(self) -> None:
        """Seed tickets and related entities."""
        self._create_ticket_tiers()
        self._create_tickets()
        self._create_payments()

    def _create_ticket_tiers(self) -> None:
        """Create ticket tiers with all payment methods and PWYC support."""
        self.log("Creating ticket tiers...")

        tiers_to_create: list[TicketTier] = []
        min_tiers, max_tiers = self.config.tiers_per_event

        for event in self.state.ticketed_events:
            num_tiers = self.random_int(min_tiers, max_tiers)
            tier_names = self.random_sample(TIER_NAMES, num_tiers)

            # Get event's venue and sectors if available
            event_venue = event.venue
            event_sectors = self.state.venue_sectors.get(event_venue.id, []) if event_venue else []

            event_tiers: list[TicketTier] = []
            for i, name in enumerate(tier_names):
                # Payment method distribution
                payment_key = self.weighted_choice(self.config.payment_method_weights)
                payment_map = {
                    "online": TicketTier.PaymentMethod.ONLINE,
                    "offline": TicketTier.PaymentMethod.OFFLINE,
                    "at_the_door": TicketTier.PaymentMethod.AT_THE_DOOR,
                    "free": TicketTier.PaymentMethod.FREE,
                }
                payment_method = payment_map[payment_key]

                # PWYC for ~20% of non-free tiers
                if payment_method != TicketTier.PaymentMethod.FREE:
                    is_pwyc = self.random_bool(self.config.pwyc_tier_pct)
                else:
                    is_pwyc = False

                price_type = TicketTier.PriceType.PWYC if is_pwyc else TicketTier.PriceType.FIXED

                # Price based on payment method
                if payment_method == TicketTier.PaymentMethod.FREE:
                    price = Decimal("0")
                else:
                    price = Decimal(self.random_int(10, 150))

                # Visibility distribution
                visibility_key = self.weighted_choice(
                    {
                        "public": 0.6,
                        "private": 0.2,
                        "members_only": 0.15,
                        "staff_only": 0.05,
                    }
                )
                visibility_map = {
                    "public": TicketTier.Visibility.PUBLIC,
                    "private": TicketTier.Visibility.PRIVATE,
                    "members_only": TicketTier.Visibility.MEMBERS_ONLY,
                    "staff_only": TicketTier.Visibility.STAFF_ONLY,
                }

                # Purchasable by distribution
                purchasable_by = self.random_choice(list(TicketTier.PurchasableBy))

                # Quantity: None (unlimited) or specific values
                total_quantity = self.random_choice([None, 10, 25, 50, 100, 200])

                # Sales window
                sales_start_at = None
                sales_end_at = None
                if self.random_bool(0.4):
                    sales_start_at = event.start - timedelta(days=self.random_int(7, 30))
                if self.random_bool(0.3):
                    sales_end_at = event.start - timedelta(hours=self.random_int(1, 24))

                # Seat assignment mode
                seat_assignment_mode = TicketTier.SeatAssignmentMode.NONE
                tier_venue = None
                tier_sector = None

                # If event has venue and we have sectors, some tiers get seat assignment
                if event_venue and event_sectors and self.random_bool(0.3):
                    tier_venue = event_venue
                    tier_sector = self.random_choice(event_sectors)
                    seat_assignment_mode = self.random_choice(
                        [
                            TicketTier.SeatAssignmentMode.NONE,
                            TicketTier.SeatAssignmentMode.RANDOM,
                            TicketTier.SeatAssignmentMode.USER_CHOICE,
                        ]
                    )

                tier = TicketTier(
                    event=event,
                    name=name,
                    description=f"{name} ticket for {event.name}",
                    visibility=visibility_map[visibility_key],
                    payment_method=payment_method,
                    purchasable_by=purchasable_by,
                    price=price,
                    price_type=price_type,
                    pwyc_min=Decimal("5") if is_pwyc else Decimal("1"),
                    pwyc_max=Decimal("200") if is_pwyc else None,
                    total_quantity=total_quantity,
                    quantity_sold=0,
                    sales_start_at=sales_start_at,
                    sales_end_at=sales_end_at,
                    manual_payment_instructions=(
                        "Please transfer to IBAN: XX1234567890"
                        if payment_method == TicketTier.PaymentMethod.OFFLINE
                        else None
                    ),
                    venue=tier_venue,
                    sector=tier_sector,
                    seat_assignment_mode=seat_assignment_mode,
                    max_tickets_per_user=self.random_choice([None, 1, 2, 5]) if self.random_bool(0.3) else None,
                )
                tiers_to_create.append(tier)
                event_tiers.append(tier)

            self.state.ticket_tiers[event.id] = event_tiers

        created = self.batch_create(TicketTier, tiers_to_create, desc="Creating ticket tiers")

        # Update state with actual created tiers (with IDs)
        idx = 0
        for event in self.state.ticketed_events:
            num_tiers = len(self.state.ticket_tiers.get(event.id, []))
            self.state.ticket_tiers[event.id] = created[idx : idx + num_tiers]
            idx += num_tiers

        self.log(f"  Created {len(created)} ticket tiers")

    def _get_ticket_count(self, tier: TicketTier, is_sold_out: bool) -> int:
        """Determine the number of tickets to create for a tier."""
        if tier.total_quantity:
            return tier.total_quantity if is_sold_out else self.random_int(0, tier.total_quantity)
        return self.random_int(0, 30)

    def _get_pwyc_price(self, tier: TicketTier) -> Decimal | None:
        """Calculate PWYC price if applicable."""
        pwyc_methods = [TicketTier.PaymentMethod.OFFLINE, TicketTier.PaymentMethod.AT_THE_DOOR]
        if tier.price_type == TicketTier.PriceType.PWYC and tier.payment_method in pwyc_methods:
            return Decimal(self.random_int(int(tier.pwyc_min), int(tier.pwyc_max or 100)))
        return None

    def _create_tickets(self) -> None:
        """Create tickets with various statuses, including sold-out scenarios."""
        self.log("Creating tickets...")

        tickets_to_create: list[Ticket] = []

        # Track which events should be sold out
        sold_out_target = int(len(self.state.ticketed_events) * self.config.sold_out_event_pct)
        events_to_sell_out = self.random_sample(self.state.ticketed_events, sold_out_target)
        sold_out_event_ids = {e.id for e in events_to_sell_out}

        for event in self.state.ticketed_events:
            event_tiers = self.state.ticket_tiers.get(event.id, [])
            if not event_tiers:
                continue

            is_sold_out = event.id in sold_out_event_ids

            for tier in event_tiers:
                num_tickets = self._get_ticket_count(tier, is_sold_out)
                if num_tickets == 0:
                    continue

                ticket_users = self.random_sample(self.state.users, min(num_tickets, len(self.state.users)))

                for user in ticket_users:
                    status = TICKET_STATUS_MAP[self.weighted_choice(self.config.ticket_status_weights)]
                    checked_in_at = None
                    checked_in_by = None

                    if status == Ticket.TicketStatus.CHECKED_IN:
                        checked_in_at = event.start + timedelta(minutes=self.random_int(0, 60))
                        checked_in_by = event.organization.owner

                    ticket = Ticket(
                        event=event,
                        user=user,
                        tier=tier,
                        status=status,
                        guest_name=user.get_display_name(),
                        checked_in_at=checked_in_at,
                        checked_in_by=checked_in_by,
                        price_paid=self._get_pwyc_price(tier),
                        venue=tier.venue,
                        sector=tier.sector,
                    )
                    tickets_to_create.append(ticket)

        self.batch_create(Ticket, tickets_to_create, desc="Creating tickets")
        self.log(f"  Created {len(tickets_to_create)} tickets")

        self._update_tier_quantities()

        self.state.sold_out_events.extend(events_to_sell_out)
        self.log(f"  Sold out events: {len(self.state.sold_out_events)}")

    def _update_tier_quantities(self) -> None:
        """Update quantity_sold for each tier based on created tickets."""
        self.log("Updating tier quantities...")

        from django.db.models import Count

        # Get ticket counts per tier (excluding cancelled)
        tier_counts = (
            Ticket.objects.exclude(status=Ticket.TicketStatus.CANCELLED).values("tier_id").annotate(count=Count("id"))
        )

        tier_count_map = {tc["tier_id"]: tc["count"] for tc in tier_counts}

        # Update tiers in bulk
        tiers_to_update: list[TicketTier] = []
        for event_tiers in self.state.ticket_tiers.values():
            for tier in event_tiers:
                count = tier_count_map.get(tier.id, 0)
                if count != tier.quantity_sold:
                    tier.quantity_sold = count
                    tiers_to_update.append(tier)

        if tiers_to_update:
            TicketTier.objects.bulk_update(tiers_to_update, ["quantity_sold"])
            self.log(f"  Updated {len(tiers_to_update)} tier quantities")

    def _create_payments(self) -> None:
        """Create Payment records for online tickets."""
        self.log("Creating payments...")

        payments_to_create: list[Payment] = []

        # Get tickets with online payment tiers that don't already have payments
        online_tickets = Ticket.objects.filter(
            tier__payment_method=TicketTier.PaymentMethod.ONLINE,
            payment__isnull=True,
        ).select_related("tier", "user")

        for ticket in online_tickets:
            # Status distribution
            status_key = self.weighted_choice(self.config.payment_status_weights)
            status_map = {
                "succeeded": Payment.PaymentStatus.SUCCEEDED,
                "pending": Payment.PaymentStatus.PENDING,
                "failed": Payment.PaymentStatus.FAILED,
                "refunded": Payment.PaymentStatus.REFUNDED,
            }
            status = status_map[status_key]

            # Calculate platform fee (5%)
            amount = ticket.tier.price
            platform_fee = amount * Decimal("0.05")

            payments_to_create.append(
                Payment(
                    ticket=ticket,
                    user=ticket.user,
                    stripe_session_id=f"cs_test_{uuid.uuid4().hex[:24]}",
                    stripe_payment_intent_id=f"pi_test_{uuid.uuid4().hex[:24]}"
                    if status != Payment.PaymentStatus.PENDING
                    else None,
                    status=status,
                    amount=amount,
                    platform_fee=platform_fee,
                    currency=ticket.tier.currency,
                    expires_at=timezone.now() + timedelta(minutes=30),
                )
            )

        self.batch_create(Payment, payments_to_create, desc="Creating payments")
        self.log(f"  Created {len(payments_to_create)} payments")
