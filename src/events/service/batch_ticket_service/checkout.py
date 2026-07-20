"""One method per payment method — what happens once the cart is priced.

Each ``_*_checkout`` receives an already validated, seated and priced cart and
decides only three things: the status the tickets get, whether Payment rows are
created, and which side effects fire. ``create_batch`` picks the method.
"""

import typing as t
from uuid import UUID

from django.db.models import F

from events.models import Ticket, TicketTier, VenueSeat
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service.tickets import TicketWriterMixin
from events.service.seating.pricing import BatchPricing

if t.TYPE_CHECKING:
    from events.schema.ticket import BuyerBillingInfoSchema


class CheckoutMixin(TicketWriterMixin):
    """The per-payment-method terminal steps of ``create_batch``."""

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

        # PENDING tickets; price_paid stays NULL online — Payment.amount is authoritative (spec §5.5).
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
