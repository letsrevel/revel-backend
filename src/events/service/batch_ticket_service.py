"""Service for batch ticket purchases with seat selection support."""

import copy
import typing as t
from decimal import Decimal
from uuid import UUID

import structlog
from django.db import transaction
from django.db.models import F
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventSeatOverride,
    OrganizationMember,
    SeatHold,
    Ticket,
    TicketTier,
    VenueSeat,
    WaitlistOffer,
)
from events.models.discount_code import DiscountCode
from events.schema import TicketPurchaseItem
from events.service.discount_code_service import assert_min_purchase_amount
from events.service.seating import holds as holds_service
from events.service.seating.pricing import BatchPricing, TicketPrice, build_batch_pricing, cart_is_certainly_free
from events.tasks import build_attendee_visibility_flags
from notifications.signals.ticket import send_batch_ticket_created_notifications
from notifications.signals.waitlist import remove_user_from_waitlist

if t.TYPE_CHECKING:
    from events.schema.ticket import BuyerBillingInfoSchema
    from events.service.attendee_vat_service import BuyerVATContext

logger = structlog.get_logger(__name__)


class _SeatConflictError(Exception):
    """Internal retry signal: an optimistically picked seat block was invalidated after locking.

    Raised inside the per-attempt savepoint in ``_resolve_seats_best_available`` so the
    savepoint rollback releases that attempt's row locks; never escapes the retry loop.
    Deliberately not an HttpError — it must be caught locally, not rendered.
    """


class BatchTicketService:
    """Service for creating multiple tickets in a single transaction.

    Handles:
    - Batch size validation against max_tickets_per_user limits
    - Seat resolution (NONE, BEST_AVAILABLE, USER_CHOICE modes)
    - Atomic ticket creation
    - Payment flow delegation (online, offline, free)
    """

    def __init__(
        self,
        event: Event,
        tier: TicketTier,
        user: RevelUser,
        discount_code: DiscountCode | None = None,
        *,
        guest_session: str | None = None,
        accessible_required: bool = False,
    ) -> None:
        """Initialize the batch ticket service.

        Args:
            event: The event for which tickets are being purchased.
            tier: The ticket tier being purchased.
            user: The user purchasing the tickets.
            discount_code: Optional validated discount code to apply.
            guest_session: Guest-hold session id for guest checkout — the browser
                held seats under this identity, not under the guest RevelUser.
            accessible_required: BEST_AVAILABLE assignment must use the accessible
                seat pool (relaxed contiguity) for the whole batch (#726).
        """
        self.event = event
        self.tier = tier
        self.user = user
        self.discount_code = discount_code
        self.guest_session = guest_session
        self.accessible_required = accessible_required
        self._reserve_buyer_vat: "BuyerVATContext | None" = None

    def _assert_purchasable_by(self) -> None:
        """Assert the user is allowed to purchase from this tier.

        Checks the tier's purchasable_by setting and, when restrict_purchase_to_linked_invitations
        is True, verifies the user's invitation links to this specific tier.

        Staff and org owners are exempt from purchasable_by restrictions (consistent with
        CanPurchaseTicket permission). They can always purchase from any tier on their events.
        """
        PB = TicketTier.PurchasableBy
        if self.tier.purchasable_by == PB.PUBLIC:
            return

        org = self.event.organization
        if org.is_owner_or_staff(self.user):
            return

        is_member = OrganizationMember.objects.active_only().filter(organization=org, user=self.user).exists()
        invitation = EventInvitation.objects.filter(event=self.event, user=self.user).first()

        def _invited_passes() -> bool:
            if invitation is None:
                return False
            if self.tier.restrict_purchase_to_linked_invitations:
                return invitation.tiers.filter(pk=self.tier.pk).exists()
            return True

        if self.tier.purchasable_by == PB.MEMBERS and is_member:
            return
        if self.tier.purchasable_by == PB.INVITED and _invited_passes():
            return
        if self.tier.purchasable_by == PB.INVITED_AND_MEMBERS and (is_member or _invited_passes()):
            return

        raise HttpError(403, str(_("You are not allowed to purchase from this tier.")))

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
        because it's checked separately by assert_tier_capacity with proper
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

    def _resolve_seats_none(self, count: int) -> list[VenueSeat | None]:
        """No seat assignment (GA/standing)."""
        return [None] * count

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

        # Lock and fetch the requested seats, in PK order to match the global
        # seat-lock protocol (holds/overrides), avoiding cross-path deadlocks.
        seats = list(
            VenueSeat.objects.filter(
                id__in=seat_ids,
                sector_id=self.tier.sector_id,
                is_active=True,
            )
            .order_by("pk")
            .select_for_update()
        )

        if len(seats) != len(seat_ids):
            raise HttpError(
                400,
                str(_("One or more selected seats are invalid or not in the correct sector.")),
            )

        # Check none are already taken or under a box-office override (held/killed).
        # Occupancy matches the unique_ticket_event_seat constraint predicate:
        # any non-cancelled ticket (incl. CHECKED_IN) occupies the seat.
        taken = (
            Ticket.objects.filter(event=self.event, seat_id__in=seat_ids)
            .exclude(status=Ticket.TicketStatus.CANCELLED)
            .exists()
        )
        overridden = EventSeatOverride.objects.filter(event=self.event, seat_id__in=seat_ids).exists()

        if taken or overridden:
            raise HttpError(
                400,
                str(_("One or more selected seats are no longer available.")),
            )

        # Holds are advisory: reject seats live-held by ANOTHER identity, consume our own
        try:
            self._verify_and_consume_holds([s.id for s in seats])
        except holds_service.SeatHoldConflictError:
            raise HttpError(409, str(_("One or more selected seats are held by another buyer."))) from None

        # Return seats in the same order as requested
        seat_map = {s.id: s for s in seats}
        return [seat_map[sid] for sid in seat_ids if sid is not None]

    def _verify_and_consume_holds(self, seat_ids: list[UUID]) -> None:
        """Reject seats live-held by another identity; delete the buyer's own holds.

        Guest checkout runs as a guest RevelUser, but the browser held its seats
        under the guest session — when one is present it IS the hold identity
        (a RevelUser instance always reads as authenticated to owner_q).

        Raises:
            holds_service.SeatHoldConflictError: If a seat is live-held by another identity.
        """
        holds_service.verify_and_consume_holds(
            self.event,
            seat_ids,
            user=None if self.guest_session else self.user,
            guest_session=self.guest_session,
        )

    def _lock_and_verify_block(self, picked_ids: list[UUID]) -> list[VenueSeat]:
        """Lock a picked seat block PK-ordered and re-verify it under the lock.

        Must run inside a savepoint (nested ``transaction.atomic()``): raises
        ``_SeatConflictError`` when a seat vanished/deactivated, got ticketed,
        was box-office overridden, or is live-held by another identity — the
        caller's savepoint rollback then releases this attempt's row locks.
        On success the buyer's own holds on the block are consumed.

        Args:
            picked_ids: Seat ids to secure, in desired assignment order.

        Returns:
            The locked VenueSeat rows in ``picked_ids`` order.

        Raises:
            _SeatConflictError: If any seat can no longer be assigned.
        """
        # Locked in PK order per the global seat-lock protocol (holds/overrides).
        # is_active is re-evaluated against the locked row version (EvalPlanQual),
        # so a just-deactivated seat drops out here as a length mismatch.
        seats = list(VenueSeat.objects.filter(id__in=picked_ids, is_active=True).order_by("pk").select_for_update())
        if len(seats) != len(picked_ids):  # a picked seat vanished/deactivated — re-pick
            raise _SeatConflictError
        # Non-cancelled = occupied, matching unique_ticket_event_seat.
        taken = (
            Ticket.objects.filter(event=self.event, seat_id__in=picked_ids)
            .exclude(status=Ticket.TicketStatus.CANCELLED)
            .exists()
        )
        if taken:
            raise _SeatConflictError
        # Post-lock override re-check: the pick/hold read overrides unlocked, so a
        # seat killed/held by box office in between must be caught here (mirrors
        # _resolve_seats_user_choice).
        if EventSeatOverride.objects.filter(event=self.event, seat_id__in=picked_ids).exists():
            raise _SeatConflictError
        try:
            self._verify_and_consume_holds(picked_ids)
        except holds_service.SeatHoldConflictError:
            raise _SeatConflictError from None
        id_order = {sid: i for i, sid in enumerate(picked_ids)}
        return sorted(seats, key=lambda s: id_order[s.id])

    def _try_consume_held_block(self, count: int) -> list[VenueSeat] | None:
        """Consume the buyer's own held seats directly instead of re-running the picker.

        A buyer who pre-held a block (e.g. via POST /seating/holds/best-available)
        must get EXACTLY the seats they were shown: re-running the picker could
        settle on a different equally-scored block (seeded tiebreak), stranding
        the original holds until TTL, and an accessible held block would be
        skipped entirely when checkout doesn't set ``accessible_required``.

        The buyer's ACTIVE holds on seats in the tier's price category (same
        identity rule as ``_verify_and_consume_holds``) are taken in deterministic
        adjacency order — (sector display_order, row_order, adjacency_index) —
        first ``count`` of them. Contiguity is deliberately NOT enforced: the
        buyer explicitly holds these exact seats (a best-available hold block is
        already adjacent) and gets exactly what they held. Accessible held seats
        are likewise consumed without ``accessible_required`` at checkout.

        Returns:
            The seats to assign, or None to fall through to the normal picker:
            when the buyer holds fewer than ``count`` matching seats, or a held
            seat conflicts post-lock (ticketed/overridden/deactivated/sniped) —
            the savepoint rollback releases the locks and restores the holds.
        """
        if not self.tier.price_category_id:
            return None
        owner_q = SeatHold.owner_q(None if self.guest_session else self.user, self.guest_session)
        held_ids = list(
            SeatHold.objects.active()
            .filter(owner_q, event=self.event, seat__default_price_category_id=self.tier.price_category_id)
            .order_by("seat__sector__display_order", "seat__row_order", "seat__adjacency_index")
            .values_list("seat_id", flat=True)
        )
        if len(held_ids) < count:
            return None
        try:
            with transaction.atomic():  # savepoint: rollback releases locks + restores holds
                return self._lock_and_verify_block(held_ids[:count])
        except _SeatConflictError:
            return None

    def _resolve_seats_best_available(self, count: int) -> list[VenueSeat]:
        """Adjacency-aware assignment: optimistic pick, then lock + verify the chosen seats only.

        When the buyer already holds enough seats for this tier's price category,
        the exact held block is consumed instead of re-running the picker (see
        ``_try_consume_held_block``); the picker below only runs when there is no
        (sufficient, still-valid) own hold.

        Retries with a fresh read when a picked seat got ticketed, overridden,
        deactivated, or foreign-held between the unlocked pick and the lock.
        Each attempt's lock + re-check runs inside its own savepoint (a nested
        ``transaction.atomic()`` under the request transaction): a conflicted
        attempt raises ``_SeatConflictError`` inside the block, and the savepoint
        rollback releases that attempt's seat locks before the next attempt locks
        a possibly different block. Without this, locks from a failed attempt
        persist to end of transaction, breaking the global PK lock-ordering
        protocol and deadlocking against concurrent purchases. A successful
        attempt exits the block normally (savepoint released, not rolled back),
        so the outer create_batch transaction keeps the locks and consumed holds.

        Args:
            count: Number of seats to assign.

        Returns:
            List of assigned VenueSeat objects in picked (adjacent) order.

        Raises:
            HttpError: 409 when no adjacent block of `count` seats can be secured.
        """
        from events.service.seating.best_available import pick_best_available
        from events.service.seating.pick import load_candidates

        if (held := self._try_consume_held_block(count)) is not None:
            return held

        for _attempt in range(3):
            # Same identity rule as _verify_and_consume_holds: a guest session, when
            # present, IS the hold identity — the buyer's own holds stay candidates.
            candidates = load_candidates(
                self.event,
                self.tier,
                exclude=set(),
                hold_owner_user=None if self.guest_session else self.user,
                hold_owner_guest_session=self.guest_session,
            )
            picked_ids = pick_best_available(candidates, count, accessible_required=self.accessible_required)
            if not picked_ids:
                if self.accessible_required:
                    raise HttpError(
                        409, str(_("Not enough accessible seats available — please contact the organizer."))
                    )
                raise HttpError(409, str(_("Not enough adjacent seats available for this tier.")))
            try:
                with transaction.atomic():  # savepoint per attempt (see docstring)
                    return self._lock_and_verify_block(picked_ids)
            except _SeatConflictError:
                continue
        raise HttpError(409, str(_("Could not secure adjacent seats — please try again.")))

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

        if mode == TicketTier.SeatAssignmentMode.BEST_AVAILABLE:
            best: list[VenueSeat | None] = list(self._resolve_seats_best_available(len(items)))
            return best

        if mode == TicketTier.SeatAssignmentMode.USER_CHOICE:
            # Cast to satisfy mypy - USER_CHOICE returns list[VenueSeat], which is a subtype
            user_seats: list[VenueSeat | None] = list(self._resolve_seats_user_choice(items))
            return user_seats

        raise HttpError(400, str(_("Unknown seat assignment mode.")))

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

    def _cart_is_certainly_free(self, pwyc_amount: Decimal | None) -> bool:
        """Whether no ticket in this cart can cost anything — see ``pricing.cart_is_certainly_free``."""
        return cart_is_certainly_free(self.tier, pwyc_amount=pwyc_amount, discount_code=self.discount_code)

    @transaction.atomic
    def create_batch(
        self,
        items: list[TicketPurchaseItem],
        pwyc_amount: Decimal | None = None,
        billing_info: "BuyerBillingInfoSchema | None" = None,
    ) -> list[Ticket] | tuple[list[Ticket], UUID]:
        """Create a batch of tickets.

        For online payment tiers, reserves the batch (PENDING tickets + PENDING
        Payment rows) and returns the tickets with a reservation_id.
        For free/offline/at-the-door tiers, returns the created tickets.

        Args:
            items: List of ticket purchase items with guest_name and optional seat_id.
            pwyc_amount: The buyer's pay-what-you-can amount. **PWYC only** — a
                discount is no longer pre-computed into this parameter by callers;
                pass the validated code as ``discount_code`` to the constructor and
                the pricing service applies it per ticket.
            billing_info: Optional buyer billing info for attendee invoicing.

        Returns:
            Either a `(tickets, reservation_id)` tuple for the ONLINE payment
            method, or a list of created Tickets for free/offline/at-the-door.

        Raises:
            HttpError: If validation fails or ticket creation fails.
        """
        # Validate purchasability (invitation-linked restrictions, membership, etc.)
        self._assert_purchasable_by()

        # Validate batch size
        self.validate_batch_size(len(items))

        # Resolve the buyer's VAT context (incl. the VIES round-trip) BEFORE
        # locking the tier, so the contended row is never held across VIES
        # (#632). Price-independent: the arithmetic runs post-lock against the
        # locked tier's fresh price. Only the paid-online path creates Stripe
        # Payment rows; other methods skip it.
        buyer_vat = None
        if self.tier.payment_method == TicketTier.PaymentMethod.ONLINE and not self._cart_is_certainly_free(
            pwyc_amount
        ):
            from events.service import stripe_service

            buyer_vat = stripe_service.resolve_attendee_vat_for_reserve(billing_info=billing_info)
        self._reserve_buyer_vat = buyer_vat  # consumed by _online_checkout

        # Lock the tier for capacity check
        locked_tier = TicketTier.objects.select_for_update().get(pk=self.tier.pk)

        # Check tier capacity
        self.assert_tier_capacity(locked_tier, len(items))

        # Check event capacity (effective_capacity - soft limit)
        self.assert_event_capacity(len(items))

        # Check sector capacity for GA tiers (hard limit - cannot be overridden)
        self._assert_sector_capacity(len(items))

        # Resolve seats
        seats = self.resolve_seats(items)

        # Price every ticket. Single source of truth for PWYC *and* discounts, and
        # the only place that reads the tier's category map. Priced off the LOCKED
        # tier, so a concurrent repricing can't be undercut by a stale pre-lock read.
        pricing = build_batch_pricing(locked_tier, seats, pwyc_amount=pwyc_amount, discount_code=self.discount_code)
        # min_purchase_amount is enforced HERE, not in validate_discount_code: only now
        # is the cart's real total known (spec §5.6).
        if self.discount_code is not None:
            assert_min_purchase_amount(self.discount_code, pricing.gross_total)
        # A NULL price_paid means "tier.price still reconstructs it", and the revenue
        # report leans on that NULL (revenue_aggregation.py:346). Stamp an explicit
        # amount only when the buyer moved the price (PWYC/discount) or the tier
        # prices seats per category (spec §5.5).
        stamp_price_paid = (
            pwyc_amount is not None or self.discount_code is not None or bool(locked_tier.category_prices)
        )

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

        # If the buyer's input drives EVERY ticket to zero, an ONLINE tier becomes a
        # free checkout. All-or-nothing over the vector: a cart mixing 0.00 and
        # positive units stays on the paid path so each ticket keeps its 1:1 Payment
        # row (the refund matcher relies on that pairing). A zero-priced ONLINE tier
        # with no PWYC/discount input is still a misconfiguration, not a free tier —
        # it keeps falling through to the 400 in reserve_batch_payments.
        buyer_reduced_price = pwyc_amount is not None or self.discount_code is not None
        if (
            locked_tier.payment_method == TicketTier.PaymentMethod.ONLINE
            and buyer_reduced_price
            and pricing.lines
            and all(line.unit_price <= 0 for line in pricing.lines)
        ):
            return self._free_checkout(items, seats, locked_tier, pricing)

        # Delegate to payment-specific method
        match locked_tier.payment_method:
            case TicketTier.PaymentMethod.ONLINE:
                return self._online_checkout(items, seats, locked_tier, pricing, billing_info)
            case TicketTier.PaymentMethod.OFFLINE:
                return self._offline_checkout(items, seats, locked_tier, pricing, stamp_price_paid)
            case TicketTier.PaymentMethod.AT_THE_DOOR:
                return self._at_the_door_checkout(items, seats, locked_tier, pricing, stamp_price_paid)
            case TicketTier.PaymentMethod.FREE:
                return self._free_checkout(items, seats, locked_tier, pricing)
            case _:
                raise HttpError(400, str(_("Unknown payment method.")))

    def create_tickets(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        status: Ticket.TicketStatus,
        lines: list[TicketPrice],
        *,
        stamp_price_paid: bool = False,
    ) -> list[Ticket]:
        """Create ticket objects with the specified status.

        ``price_paid`` and ``discount_amount`` are stamped **per ticket** from the
        price vector; on a mixed cart both legitimately differ row to row.
        ``discount_amount`` stays NULL when there is no discount code — the vector
        carries ``0.00`` there, but the column's "no code was applied" meaning is
        what the revenue detail sheet reads.

        Args:
            items: List of ticket purchase items.
            seats: List of seats (or None) corresponding to items.
            status: The status to set on created tickets.
            lines: Per-ticket prices, positionally aligned with ``items``.
            stamp_price_paid: Whether to write the unit price to ``price_paid``.
                False online (``Payment.amount`` is authoritative — spec §5.5) and
                for a plain flat-tier purchase (NULL means "``tier.price`` stands").

        Returns:
            List of created Ticket objects.
        """
        dc = self.discount_code

        tickets = []
        for item, seat, line in zip(items, seats, lines, strict=True):
            ticket = Ticket(
                event=self.event,
                tier=self.tier,
                user=self.user,
                status=status,
                guest_name=item.guest_name,
                price_paid=line.unit_price if stamp_price_paid else None,
                discount_code=dc,
                discount_amount=line.discount_amount if dc is not None else None,
                # Deep-copy the JSON snapshot so the ticket row doesn't share a dict
                # reference with the live tier. Protects the "immutable snapshot"
                # contract against future in-place mutation of tier.refund_policy.
                refund_policy_snapshot=(copy.deepcopy(self.tier.refund_policy) if self.tier.refund_policy else None),
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
            ticket.clean_fields(
                exclude=["event", "tier", "user", "seat", "sector", "venue", "discount_code", "refund_policy_snapshot"]
            )
            ticket.clean()
            tickets.append(ticket)

        created = Ticket.objects.bulk_create(tickets)

        # Apply discount usage increment after successful ticket creation
        if dc is not None:
            from events.service import discount_code_service

            discount_code_service.apply_discount(dc, self.user, len(created))

        self._claim_waitlist_offer_if_any()
        return created

    def _claim_waitlist_offer_if_any(self) -> None:
        """Mark a pending unexpired WaitlistOffer for this (event, user) as CLAIMED.

        Mirrors EventManager._claim_active_offer for ticket purchase flows.
        Fires on PENDING-ticket creation (online checkout) as well as active
        ticket creation (free/offline/at-the-door) because the PENDING ticket
        already counts toward capacity — without claiming the offer here the
        user would consume two capacity slots. No-op when the user has no
        pending offer.
        """
        from django.utils import timezone

        from events.models import EventWaitList, WaitlistOffer

        now = timezone.now()
        offer = (
            WaitlistOffer.objects.select_for_update()
            .filter(
                event=self.event,
                user=self.user,
                status=WaitlistOffer.WaitlistOfferStatus.PENDING,
                expires_at__gt=now,
            )
            .first()
        )
        if offer is None:
            return
        offer.status = WaitlistOffer.WaitlistOfferStatus.CLAIMED
        offer.claimed_at = now
        offer.save(update_fields=["status", "claimed_at"])
        EventWaitList.objects.filter(event=self.event, user=self.user).delete()
        # Defensive nudge — see EventManager._claim_active_offer for rationale.
        from events.service.waitlist_service import enqueue_waitlist_processing

        enqueue_waitlist_processing(self.event.id)

    def _online_checkout(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        locked_tier: TicketTier,
        pricing: BatchPricing,
        billing_info: "BuyerBillingInfoSchema | None" = None,
    ) -> tuple[list[Ticket], UUID]:
        """Reserve an online batch: PENDING tickets + PENDING Payment rows (#632).

        Does NOT call Stripe — the caller returns the reservation_id to the
        client, which then calls the checkout-session endpoint. Keeping Stripe
        out of this method is what lets the request commit and release the tier
        lock before the ~2.5s Session.create round-trip. Attendee VAT (VIES) was
        already resolved before the lock in create_batch and is passed through.

        Args:
            items: List of ticket purchase items.
            seats: List of seats corresponding to items.
            locked_tier: The locked tier.
            pricing: The per-ticket price vector for this cart.
            billing_info: Optional buyer billing info for attendee invoicing.

        Returns:
            Tuple of the created PENDING tickets and the reservation_id.
        """
        from uuid import uuid4

        from events.service import stripe_service

        reservation_id = uuid4()

        # Create PENDING tickets. price_paid stays NULL online — Payment.amount is
        # the authoritative number there (spec §5.5).
        tickets = self.create_tickets(items, seats, Ticket.TicketStatus.PENDING, pricing.lines)

        # Update quantity sold
        TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + len(items))

        # Create PENDING Payment rows for the reservation (no Stripe call).
        stripe_service.reserve_batch_payments(
            event=self.event,
            tier=locked_tier,
            user=self.user,
            tickets=tickets,
            reservation_id=reservation_id,
            lines=pricing.lines,
            billing_info=billing_info,
            buyer_vat_context=self._reserve_buyer_vat,
        )

        return tickets, reservation_id

    def trigger_bulk_create_side_effects(self, tickets: list[Ticket]) -> None:
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
            remove_user_from_waitlist(self.event.id, self.user.id)

        transaction.on_commit(on_commit)

    def _offline_checkout(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        locked_tier: TicketTier,
        pricing: BatchPricing,
        stamp_price_paid: bool,
    ) -> list[Ticket]:
        """Handle offline checkout for batch tickets.

        Creates PENDING tickets that need manual confirmation.

        Args:
            items: List of ticket purchase items.
            seats: List of seats corresponding to items.
            locked_tier: The locked tier.
            pricing: The per-ticket price vector for this cart.
            stamp_price_paid: Whether the unit price is written to ``price_paid``.

        Returns:
            List of created PENDING tickets.
        """
        tickets = self.create_tickets(
            items, seats, Ticket.TicketStatus.PENDING, pricing.lines, stamp_price_paid=stamp_price_paid
        )

        # Update quantity sold
        TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + len(items))

        # Trigger side effects that bulk_create doesn't handle
        self.trigger_bulk_create_side_effects(tickets)

        return tickets

    def _at_the_door_checkout(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        locked_tier: TicketTier,
        pricing: BatchPricing,
        stamp_price_paid: bool,
    ) -> list[Ticket]:
        """Handle at-the-door checkout for batch tickets.

        Creates ACTIVE tickets immediately. AT_THE_DOOR represents a commitment
        to attend (pay at arrival), so tickets count toward attendee_count.

        Args:
            items: List of ticket purchase items.
            seats: List of seats corresponding to items.
            locked_tier: The locked tier.
            pricing: The per-ticket price vector for this cart.
            stamp_price_paid: Whether the unit price is written to ``price_paid``.

        Returns:
            List of created ACTIVE tickets.
        """
        tickets = self.create_tickets(
            items, seats, Ticket.TicketStatus.ACTIVE, pricing.lines, stamp_price_paid=stamp_price_paid
        )

        # Update quantity sold
        TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + len(items))

        # Trigger side effects that bulk_create doesn't handle
        self.trigger_bulk_create_side_effects(tickets)

        return tickets

    def _free_checkout(
        self,
        items: list[TicketPurchaseItem],
        seats: list[VenueSeat | None],
        locked_tier: TicketTier,
        pricing: BatchPricing,
    ) -> list[Ticket]:
        """Handle free checkout for batch tickets.

        Creates ACTIVE tickets immediately.

        Args:
            items: List of ticket purchase items.
            seats: List of seats corresponding to items.
            locked_tier: The locked tier.
            pricing: The per-ticket price vector (all zero, or a zeroing discount).

        Returns:
            List of created ACTIVE tickets.
        """
        tickets = self.create_tickets(items, seats, Ticket.TicketStatus.ACTIVE, pricing.lines)

        # Update quantity sold
        TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + len(items))

        # Trigger side effects that bulk_create doesn't handle
        self.trigger_bulk_create_side_effects(tickets)

        return tickets
