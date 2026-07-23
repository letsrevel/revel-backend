"""Writing the ticket rows — ``bulk_create`` and the side effects it skips.

Everything that touches the ``Ticket`` table for a batch lives here: stamping the
money columns from the price vector, claiming a waitlist offer, and firing the
post_save side effects ``bulk_create`` does not.
"""

import copy

from django.db import transaction

from events.models import Ticket, VenueSeat
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service.context import BatchTicketContext
from events.service.seating.pricing import TicketPrice
from events.tasks import build_attendee_visibility_flags
from notifications.signals.ticket import send_batch_ticket_created_notifications
from notifications.signals.waitlist import remove_user_from_waitlist


class TicketWriterMixin(BatchTicketContext):
    """Create the batch's ``Ticket`` rows and run what ``bulk_create`` skips."""

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
            stamp_price_paid: Whether to write the unit price to ``price_paid``. Never
                decided here — ask ``pricing.should_stamp_price_paid`` (spec §5.5).

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
