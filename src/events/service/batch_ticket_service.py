"""Service for batch ticket purchases with seat selection support."""

from decimal import Decimal

import structlog
from django.db import transaction
from django.db.models import F
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketTier, VenueSeat
from events.schema import TicketPurchaseItem
from events.tasks import build_attendee_visibility_flags
from notifications.signals.ticket import send_batch_ticket_created_notifications
from notifications.signals.waitlist import _remove_user_from_waitlist

logger = structlog.get_logger(__name__)


class BatchTicketService:
    """Service for creating multiple tickets in a single transaction.

    Handles:
    - Batch size validation against max_tickets_per_user limits
    - Seat resolution (NONE, RANDOM, USER_CHOICE modes)
    - Atomic ticket creation
    - Payment flow delegation (online, offline, free)
    """

    def __init__(self, event: Event, tier: TicketTier, user: RevelUser) -> None:
        """Initialize the batch ticket service.

        Args:
            event: The event for which tickets are being purchased.
            tier: The ticket tier being purchased.
            user: The user purchasing the tickets.
        """
        self.event = event
        self.tier = tier
        self.user = user

    def get_user_ticket_count(self) -> int:
        """Get count of user's existing non-cancelled tickets for this tier.

        Returns:
            Number of PENDING + ACTIVE tickets the user has for this tier.
        """
        return Ticket.objects.filter(
            event=self.event,
            tier=self.tier,
            user=self.user,
            status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
        ).count()

    def get_remaining_tickets(
        self,
        event_capacity_remaining: int | None = None,
        user_ticket_count: int | None = None,
    ) -> int | None:
        """Get how many more tickets user can purchase for this tier.

        Calculates the minimum of:
        1. Per-user limit (tier-specific or event-level fallback)
        2. Event capacity remaining (if provided)

        Note: Tier capacity (total_quantity - quantity_sold) is NOT included here
        because it's checked separately by _assert_tier_capacity with proper
        "sold out" error handling (429 status code).

        Args:
            event_capacity_remaining: Remaining event capacity. None means unlimited
                or not provided. Pass this when you've pre-calculated the event's
                remaining capacity to avoid redundant queries.
            user_ticket_count: Pre-computed count of user's tickets for this tier.
                If None, will query the database. Pass this when calling in a loop
                to avoid N+1 queries.

        Returns:
            Number of remaining tickets, or None if all limits are unlimited.
        """
        limits: list[int] = []

        # 1. Per-user limit
        max_allowed = self.tier.max_tickets_per_user
        if max_allowed is None:
            max_allowed = self.event.max_tickets_per_user
        if max_allowed is not None:
            existing = user_ticket_count if user_ticket_count is not None else self.get_user_ticket_count()
            limits.append(max(0, max_allowed - existing))

        # 2. Event capacity limit (if provided)
        if event_capacity_remaining is not None:
            limits.append(max(0, event_capacity_remaining))

        return min(limits) if limits else None

    def validate_batch_size(self, requested: int) -> None:
        """Validate that the batch size doesn't exceed limits.

        Args:
            requested: Number of tickets being requested.

        Raises:
            HttpError: If the batch size exceeds the allowed limit.
        """
        remaining = self.get_remaining_tickets()
        if remaining is not None and requested > remaining:
            if remaining == 0:
                raise HttpError(
                    400,
                    str(_("You have reached the maximum number of tickets for this tier.")),
                )
            raise HttpError(
                400,
                str(_("You can only purchase {remaining} more ticket(s) for this tier.")).format(remaining=remaining),
            )

    def _get_available_seats(self) -> list[VenueSeat]:
        """Get available seats in the tier's sector.

        Returns:
            List of available VenueSeat objects.
        """
        if not self.tier.sector_id:
            return []

        taken_ids = Ticket.objects.filter(
            event=self.event,
            seat__isnull=False,
            status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
        ).values_list("seat_id", flat=True)

        return list(
            VenueSeat.objects.filter(
                sector_id=self.tier.sector_id,
                is_active=True,
            ).exclude(id__in=taken_ids)
        )

    def _resolve_seats_none(self, count: int) -> list[VenueSeat | None]:
        """No seat assignment (GA/standing)."""
        return [None] * count

    def _resolve_seats_random(self, count: int) -> list[VenueSeat]:
        """Auto-assign random available seats.

        Args:
            count: Number of seats to assign.

        Returns:
            List of assigned VenueSeat objects.

        Raises:
            HttpError: If not enough seats are available.
        """
        # Lock seats to prevent race conditions
        available = list(
            VenueSeat.objects.filter(
                sector_id=self.tier.sector_id,
                is_active=True,
            )
            .exclude(
                id__in=Ticket.objects.filter(
                    event=self.event,
                    seat__isnull=False,
                    status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
                ).values_list("seat_id", flat=True)
            )
            .select_for_update()[:count]
        )

        if len(available) < count:
            raise HttpError(
                400,
                str(_("Not enough seats available. Only {available} seat(s) remaining.")).format(
                    available=len(available)
                ),
            )
        return available

    def _resolve_seats_user_choice(self, items: list[TicketPurchaseItem]) -> list[VenueSeat]:
        """Validate and resolve user-selected seats.

        Args:
            items: List of ticket purchase items with seat_id.

        Returns:
            List of VenueSeat objects in the same order as items.

        Raises:
            HttpError: If any seat is invalid or unavailable.
        """
        seat_ids = [item.seat_id for item in items]

        # All seats must be specified for USER_CHOICE mode
        if None in seat_ids:
            raise HttpError(
                400,
                str(_("Seat selection is required for this ticket tier.")),
            )

        # Lock and fetch the requested seats
        seats = list(
            VenueSeat.objects.filter(
                id__in=seat_ids,
                sector_id=self.tier.sector_id,
                is_active=True,
            ).select_for_update()
        )

        if len(seats) != len(seat_ids):
            raise HttpError(
                400,
                str(_("One or more selected seats are invalid or not in the correct sector.")),
            )

        # Check none are already taken
        taken = Ticket.objects.filter(
            event=self.event,
            seat_id__in=seat_ids,
            status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
        ).exists()

        if taken:
            raise HttpError(
                400,
                str(_("One or more selected seats are no longer available.")),
            )

        # Return seats in the same order as requested
        seat_map = {s.id: s for s in seats}
        return [seat_map[sid] for sid in seat_ids if sid is not None]

    def resolve_seats(self, items: list[TicketPurchaseItem]) -> list[VenueSeat | None]:
        """Resolve seats based on the tier's seat assignment mode.

        Args:
            items: List of ticket purchase items.

        Returns:
            List of VenueSeat objects (or None for NONE mode).

        Raises:
            HttpError: If seat resolution fails.
        """
        mode = self.tier.seat_assignment_mode

        if mode == TicketTier.SeatAssignmentMode.NONE:
            return self._resolve_seats_none(len(items))

        if mode == TicketTier.SeatAssignmentMode.RANDOM:
            # Cast to satisfy mypy - RANDOM returns list[VenueSeat], which is a subtype
            seats: list[VenueSeat | None] = list(self._resolve_seats_random(len(items)))
            return seats

        if mode == TicketTier.SeatAssignmentMode.USER_CHOICE:
            # Cast to satisfy mypy - USER_CHOICE returns list[VenueSeat], which is a subtype
            user_seats: list[VenueSeat | None] = list(self._resolve_seats_user_choice(items))
            return user_seats

        raise HttpError(400, str(_("Unknown seat assignment mode.")))

    def _assert_tier_capacity(self, locked_tier: TicketTier, count: int) -> None:
        """Assert that the tier has capacity for the requested tickets.

        Args:
            locked_tier: The tier with select_for_update lock.
            count: Number of tickets being requested.

        Raises:
            HttpError: If the tier is sold out or doesn't have enough capacity.
        """
        if locked_tier.total_quantity is None:
            return  # Unlimited

        available = locked_tier.total_quantity - locked_tier.quantity_sold
        if available <= 0:
            raise HttpError(429, str(_("This ticket tier is sold out.")))
        if count > available:
            raise HttpError(
                400,
                str(_("Only {available} ticket(s) remaining for this tier.")).format(available=available),
            )

    def _assert_event_capacity(self, count: int) -> None:
        """Assert that the event has capacity for the requested tickets.

        Uses effective_capacity (min of max_attendees and venue.capacity) as the soft limit.
        Uses select_for_update to prevent race conditions when multiple users
        purchase tickets simultaneously.

        Args:
            count: Number of tickets being requested.

        Raises:
            HttpError: If the event is full or doesn't have enough capacity.
        """
        effective_cap = self.event.effective_capacity
        if effective_cap == 0:
            return  # Unlimited

        # Count all non-cancelled tickets with row-level locking
        current_count = (
            Ticket.objects.select_for_update()
            .filter(event=self.event)
            .exclude(status=Ticket.TicketStatus.CANCELLED)
            .count()
        )

        available = effective_cap - current_count
        if available <= 0:
            raise HttpError(429, str(_("This event is sold out.")))
        if count > available:
            raise HttpError(
                400,
                str(_("Only {available} spot(s) remaining for this event.")).format(available=available),
            )

    def _assert_sector_capacity(self, count: int) -> None:
        """Assert that the sector has capacity for the requested tickets.

        This is a HARD limit that cannot be overridden by special invitations.
        Only applies to GA tiers (seat_assignment_mode=NONE) with a sector assigned.
        For seated tiers, capacity is implicitly enforced by available seats.

        Uses select_for_update to prevent race conditions.

        Args:
            count: Number of tickets being requested.

        Raises:
            HttpError: If the sector is full or doesn't have enough capacity.
        """
        # Only enforce for GA tiers with a sector
        if self.tier.seat_assignment_mode != TicketTier.SeatAssignmentMode.NONE:
            return  # Seated tiers are limited by available seats
        if not self.tier.sector_id:
            return  # No sector assigned

        sector = self.tier.sector
        if not sector or not sector.capacity:
            return  # No capacity limit set

        # Count all non-cancelled tickets in this sector for this event with row-level locking
        current_count = (
            Ticket.objects.select_for_update()
            .filter(event=self.event, sector=sector)
            .exclude(status=Ticket.TicketStatus.CANCELLED)
            .count()
        )

        available = sector.capacity - current_count
        if available <= 0:
            raise HttpError(429, str(_("This sector is full.")))
        if count > available:
            raise HttpError(
                400,
                str(_("Only {available} spot(s) remaining in this sector.")).format(available=available),
            )

    @transaction.atomic
    def create_batch(
        self,
        items: list[TicketPurchaseItem],
        price_override: Decimal | None = None,
    ) -> list[Ticket] | str:
        """Create a batch of tickets.

        For online payment tiers, returns a Stripe checkout URL.
        For free/offline tiers, returns the created tickets.

        Args:
            items: List of ticket purchase items with guest_name and optional seat_id.
            price_override: Price override for PWYC tiers.

        Returns:
            Either a Stripe checkout URL (str) or list of created Tickets.

        Raises:
            HttpError: If validation fails or ticket creation fails.
        """
        # Validate batch size
        self.validate_batch_size(len(items))

        # Lock the tier for capacity check
        locked_tier = TicketTier.objects.select_for_update().get(pk=self.tier.pk)

        # Check tier capacity
        self._assert_tier_capacity(locked_tier, len(items))

        # Check event capacity (effective_capacity - soft limit)
        self._assert_event_capacity(len(items))

        # Check sector capacity for GA tiers (hard limit - cannot be overridden)
        self._assert_sector_capacity(len(items))

        # Resolve seats
        seats = self.resolve_seats(items)

        # Log the batch purchase attempt for audit trail
        logger.info(
            "batch_ticket_purchase_started",
            user_id=str(self.user.id),
            event_id=str(self.event.id),
            tier_id=str(self.tier.id),
            ticket_count=len(items),
            payment_method=locked_tier.payment_method,
            seat_assignment_mode=self.tier.seat_assignment_mode,
            has_seats=any(s is not None for s in seats),
        )

        # Delegate to payment-specific method
        match locked_tier.payment_method:
            case TicketTier.PaymentMethod.ONLINE:
                return self._online_checkout(items, seats, locked_tier, price_override)
            case TicketTier.PaymentMethod.OFFLINE:
                return self._offline_checkout(items, seats, locked_tier, price_override)
            case TicketTier.PaymentMethod.AT_THE_DOOR:
                return self._at_the_door_checkout(items, seats, locked_tier, price_override)
            case TicketTier.PaymentMethod.FREE:
                return self._free_checkout(items, seats, locked_tier)
            case _:
                raise HttpError(400, str(_("Unknown payment method.")))

    def _create_tickets(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        status: Ticket.TicketStatus,
        price_paid: Decimal | None = None,
    ) -> list[Ticket]:
        """Create ticket objects with the specified status.

        Args:
            items: List of ticket purchase items.
            seats: List of seats (or None) corresponding to items.
            status: The status to set on created tickets.
            price_paid: Price paid per ticket for PWYC offline/at_the_door purchases.

        Returns:
            List of created Ticket objects.
        """
        tickets = []
        for item, seat in zip(items, seats, strict=True):
            ticket = Ticket(
                event=self.event,
                tier=self.tier,
                user=self.user,
                status=status,
                guest_name=item.guest_name,
                price_paid=price_paid,
            )
            if seat:
                ticket.seat = seat
                ticket.sector = seat.sector
                ticket.venue = seat.sector.venue
            elif self.tier.venue:
                ticket.venue = self.tier.venue
                if self.tier.sector:
                    ticket.sector = self.tier.sector

            # Skip FK validation - we've already validated event, tier, user exist
            # full_clean() would query DB to check each FK exists (3+ queries per ticket)
            ticket.clean_fields(exclude=["event", "tier", "user", "seat", "sector", "venue"])
            ticket.clean()
            tickets.append(ticket)

        return Ticket.objects.bulk_create(tickets)

    def _online_checkout(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        locked_tier: TicketTier,
        price_override: Decimal | None,
    ) -> str:
        """Handle online (Stripe) checkout for batch tickets.

        Args:
            items: List of ticket purchase items.
            seats: List of seats corresponding to items.
            locked_tier: The locked tier.
            price_override: Price override for PWYC.

        Returns:
            Stripe checkout URL.
        """
        from events.service import stripe_service

        # Create PENDING tickets
        tickets = self._create_tickets(items, seats, Ticket.TicketStatus.PENDING)

        # Update quantity sold
        TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + len(items))

        # Create Stripe checkout session
        checkout_url = stripe_service.create_batch_checkout_session(
            event=self.event,
            tier=locked_tier,
            user=self.user,
            tickets=tickets,
            price_override=price_override,
        )

        return checkout_url

    def _trigger_bulk_create_side_effects(self, tickets: list[Ticket]) -> None:
        """Trigger side effects that post_save signals would normally handle.

        Django's bulk_create does NOT trigger post_save signals, so we must
        manually trigger the necessary side effects:
        - Update attendee_count via build_attendee_visibility_flags task
        - Send ticket created notifications
        - Remove user from waitlist

        Args:
            tickets: List of tickets created via bulk_create.
        """

        def on_commit() -> None:
            # Update attendee_count (once per batch, not per ticket)
            build_attendee_visibility_flags.delay(str(self.event.id))

            # Send notifications for all tickets in batch (fetches shared data once)
            send_batch_ticket_created_notifications(tickets)

            # Remove user from waitlist (once per batch)
            _remove_user_from_waitlist(self.event.id, self.user.id)

        transaction.on_commit(on_commit)

    def _offline_checkout(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        locked_tier: TicketTier,
        price_override: Decimal | None = None,
    ) -> list[Ticket]:
        """Handle offline checkout for batch tickets.

        Creates PENDING tickets that need manual confirmation.

        Args:
            items: List of ticket purchase items.
            seats: List of seats corresponding to items.
            locked_tier: The locked tier.
            price_override: Price override for PWYC tiers.

        Returns:
            List of created PENDING tickets.
        """
        tickets = self._create_tickets(items, seats, Ticket.TicketStatus.PENDING, price_paid=price_override)

        # Update quantity sold
        TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + len(items))

        # Trigger side effects that bulk_create doesn't handle
        self._trigger_bulk_create_side_effects(tickets)

        return tickets

    def _at_the_door_checkout(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        locked_tier: TicketTier,
        price_override: Decimal | None = None,
    ) -> list[Ticket]:
        """Handle at-the-door checkout for batch tickets.

        Creates ACTIVE tickets immediately. AT_THE_DOOR represents a commitment
        to attend (pay at arrival), so tickets count toward attendee_count.

        Args:
            items: List of ticket purchase items.
            seats: List of seats corresponding to items.
            locked_tier: The locked tier.
            price_override: Price override for PWYC tiers.

        Returns:
            List of created ACTIVE tickets.
        """
        tickets = self._create_tickets(items, seats, Ticket.TicketStatus.ACTIVE, price_paid=price_override)

        # Update quantity sold
        TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + len(items))

        # Trigger side effects that bulk_create doesn't handle
        self._trigger_bulk_create_side_effects(tickets)

        return tickets

    def _free_checkout(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        locked_tier: TicketTier,
    ) -> list[Ticket]:
        """Handle free checkout for batch tickets.

        Creates ACTIVE tickets immediately.

        Args:
            items: List of ticket purchase items.
            seats: List of seats corresponding to items.
            locked_tier: The locked tier.

        Returns:
            List of created ACTIVE tickets.
        """
        tickets = self._create_tickets(items, seats, Ticket.TicketStatus.ACTIVE)

        # Update quantity sold
        TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + len(items))

        # Trigger side effects that bulk_create doesn't handle
        self._trigger_bulk_create_side_effects(tickets)

        return tickets
