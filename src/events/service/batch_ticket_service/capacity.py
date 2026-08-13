"""Is there room for these tickets? — tier, event and sector capacity assertions.

The inventory-side half of "no". Every check here takes row locks, so they run
inside ``create_batch``'s transaction and in a fixed order (tier → event → sector).
The buyer-side half — per-user limits and tier access — lives in :mod:`.eligibility`.
"""

import typing as t
from uuid import UUID

from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import Event, Ticket, TicketTier, VenueSector, WaitlistOffer
from events.service.batch_ticket_service.context import BatchTicketContext

if t.TYPE_CHECKING:
    from events.service.batch_ticket_service.context import CartGroup


class CapacityMixin(BatchTicketContext):
    """Tier, event and sector capacity assertions."""

    def assert_tier_capacity(self, locked_tier: TicketTier, count: int) -> None:
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

    def assert_event_capacity(self, count: int) -> None:
        """Assert that the event has capacity for the requested tickets.

        Uses effective_capacity (min of max_attendees and venue.capacity) as the soft limit.
        Uses select_for_update to prevent race conditions when multiple users
        purchase tickets simultaneously.

        Counts committed tickets PLUS pending unexpired waitlist offers
        (excluding cutoff-batch offers which race FCFS against real seats,
        and excluding the current user's own offer which reserves a seat
        FOR them). This mirrors EventManager._assert_capacity.

        Args:
            count: Number of tickets being requested.

        Raises:
            HttpError: If the event is full or doesn't have enough capacity.
        """
        from django.utils import timezone

        effective_cap = self.event.effective_capacity
        if effective_cap == 0:
            return  # Unlimited

        # Lock the Event row to serialize against process_waitlist_for_event
        # and other capacity-modifying flows.
        self.event = Event.objects.select_for_update().get(pk=self.event.pk)

        # Count all non-cancelled tickets with row-level locking
        current_count = (
            Ticket.objects.select_for_update()
            .filter(event=self.event)
            .exclude(status=Ticket.TicketStatus.CANCELLED)
            .count()
        )

        now = timezone.now()
        pending_offers = (
            WaitlistOffer.objects.select_for_update()
            .filter(
                event=self.event,
                status=WaitlistOffer.WaitlistOfferStatus.PENDING,
                expires_at__gt=now,
                is_cutoff_batch=False,
            )
            .count()
        )
        has_own_offer = WaitlistOffer.objects.filter(
            event=self.event,
            user=self.user,
            status=WaitlistOffer.WaitlistOfferStatus.PENDING,
            expires_at__gt=now,
            is_cutoff_batch=False,
        ).exists()
        if has_own_offer:
            pending_offers = max(0, pending_offers - 1)

        available = effective_cap - current_count - pending_offers
        if available <= 0:
            raise HttpError(429, str(_("This event is sold out.")))
        if count > available:
            raise HttpError(
                400,
                str(_("Only {available} spot(s) remaining for this event.")).format(available=available),
            )

    def assert_sector_capacities(self, groups: "list[CartGroup]") -> None:
        """Assert every sector touched by the cart's GA groups can hold their combined demand.

        This is a HARD limit that cannot be overridden by special invitations.
        Only applies to GA tiers (seat_assignment_mode=NONE) with a sector assigned.
        For seated tiers, capacity is implicitly enforced by available seats.

        Two GA tiers can share a sector; asserting per tier would let each group
        pass individually while their sum oversells the sector. Demand is summed
        per sector first, then each sector is locked-and-asserted once.

        Args:
            groups: The cart's groups (one per tier).

        Raises:
            HttpError: If a sector is full or doesn't have enough capacity for its
                cart groups' combined demand.
        """
        demand: dict[UUID, int] = {}
        sectors: dict[UUID, VenueSector] = {}
        capacities: dict[UUID, int] = {}
        for group in groups:
            tier = group.tier
            if tier.seat_assignment_mode != TicketTier.SeatAssignmentMode.NONE:
                continue  # Seated tiers are limited by available seats
            if not tier.sector_id or not tier.sector or not tier.sector.capacity:
                continue  # No sector assigned, or no capacity limit set
            demand[tier.sector_id] = demand.get(tier.sector_id, 0) + len(group.items)
            sectors[tier.sector_id] = tier.sector
            capacities[tier.sector_id] = tier.sector.capacity

        for sector_id, count in demand.items():
            sector = sectors[sector_id]
            # Count all non-cancelled tickets in this sector for this event with row-level locking
            current_count = (
                Ticket.objects.select_for_update()
                .filter(event=self.event, sector=sector)
                .exclude(status=Ticket.TicketStatus.CANCELLED)
                .count()
            )

            available = capacities[sector_id] - current_count
            if available <= 0:
                raise HttpError(429, str(_("This sector is full.")))
            if count > available:
                raise HttpError(
                    400,
                    str(_("Only {available} spot(s) remaining in this sector.")).format(available=available),
                )
