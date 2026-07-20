"""Which seats does this cart get? — the three seat assignment modes.

Owns the seat-lock protocol for checkout: locks are always taken in PK order
(matching holds/overrides), every optimistic pick is re-verified under its lock
inside a savepoint, and the buyer's own holds are consumed on success.
"""

from uuid import UUID

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import EventSeatOverride, SeatHold, Ticket, TicketTier, VenueSeat
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service.context import BatchTicketContext
from events.service.seating import holds as holds_service


class _SeatConflictError(Exception):
    """Internal retry signal: an optimistically picked seat block was invalidated after locking.

    Raised inside the per-attempt savepoint in ``_resolve_seats_best_available`` so the
    savepoint rollback releases that attempt's row locks; never escapes the retry loop.
    Deliberately not an HttpError — it must be caught locally, not rendered.
    """


class SeatResolutionMixin(BatchTicketContext):
    """Resolve the cart's seats according to the tier's ``seat_assignment_mode``."""

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
