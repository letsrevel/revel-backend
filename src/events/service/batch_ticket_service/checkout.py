"""One method per payment method — what happens once the cart is priced.

Each ``_*_checkout`` receives an already validated, seated and priced cart and
decides only three things: the status the tickets get, whether Payment rows are
created, and which side effects fire. ``create_batch`` picks the method.

Every branch takes the cart as parallel per-group lists (groups, seats, locked
tiers, pricings, and — except online — stamp flags), writes each group's tickets
against its own locked tier, and moves each tier's ``quantity_sold`` by that
group's count alone.
"""

import dataclasses
import typing as t
from uuid import UUID

from django.db.models import F

from events.models import Ticket, TicketTier, VenueSeat
from events.service.batch_ticket_service.context import CartGroup
from events.service.batch_ticket_service.tickets import TicketWriterMixin
from events.service.seating.pricing import ZERO, BatchPricing, TicketPrice

if t.TYPE_CHECKING:
    from events.schema.ticket import BuyerBillingInfoSchema


class CheckoutMixin(TicketWriterMixin):
    """The per-payment-method terminal steps of ``create_batch``."""

    def _bump_quantity_sold(self, locked_tier: TicketTier, count: int) -> None:
        """Move one tier's ``quantity_sold`` by this group's count.

        Per tier, never per cart: two groups sharing one increment would let a tier
        oversell by the other group's size.

        Args:
            locked_tier: The tier the group's tickets were written against.
            count: How many tickets that group wrote.
        """
        TicketTier.objects.filter(pk=locked_tier.pk).update(quantity_sold=F("quantity_sold") + count)

    def _online_checkout(
        self,
        groups: list[CartGroup],
        seats_per_group: list[list[VenueSeat | None]],
        locked_tiers: list[TicketTier],
        pricings: list[BatchPricing],
        billing_info: "BuyerBillingInfoSchema | None" = None,
    ) -> tuple[list[Ticket], UUID]:
        """Reserve an online batch: PENDING tickets + PENDING Payment rows (#632).

        Does NOT call Stripe — the caller returns the reservation_id to the
        client, which then calls the checkout-session endpoint. Keeping Stripe
        out of this method is what lets the request commit and release the tier
        locks before the ~2.5s Session.create round-trip. Attendee VAT (VIES) was
        already resolved before the lock in create_batch and is passed through.

        Args:
            groups: The cart's per-tier groups, in cart order.
            seats_per_group: Each group's resolved seats.
            locked_tiers: Each group's locked tier.
            pricings: Each group's price vector.
            billing_info: Optional buyer billing info for attendee invoicing.

        Returns:
            Tuple of the created PENDING tickets (cart order, every group
            concatenated) and the reservation_id.
        """
        from uuid import uuid4

        from events.service import stripe_service

        reservation_id = uuid4()

        tickets: list[Ticket] = []
        lines: list[TicketPrice] = []
        for group, seats, locked_tier, pricing in zip(groups, seats_per_group, locked_tiers, pricings, strict=True):
            # PENDING tickets; price_paid stays NULL online — PERMANENTLY (#758). Payment.amount is
            # authoritative (spec §5.5) and is net for a reverse-charge buyer, so stamping it would
            # make price_paid's meaning depend on the buyer's VAT status. Never pass stamp_price_paid.
            tickets.extend(
                self.create_tickets(
                    group.items,
                    seats,
                    Ticket.TicketStatus.PENDING,
                    pricing.lines,
                    tier=locked_tier,
                    discount_code=self._dc_for(locked_tier),
                )
            )
            lines.extend(pricing.lines)
            self._bump_quantity_sold(locked_tier, len(group.items))

        # Create PENDING Payment rows for the reservation (no Stripe call). One
        # reservation covers the whole cart, so the tickets and lines go in flattened
        # in cart order — they stay positionally aligned.
        # ponytail: reserve_batch_payments still takes ONE scalar tier, so a multi-tier
        # ONLINE cart would resolve VAT and stamp every Payment row against the FIRST
        # group's tier. Harmless today — no caller can build one (the cart-form
        # endpoints don't exist yet) — and the ceiling lifts when the per-tier
        # signature lands in the next task; until then keep ONLINE carts single-tier.
        stripe_service.reserve_batch_payments(
            event=self.event,
            tier=locked_tiers[0],
            user=self.user,
            tickets=tickets,
            reservation_id=reservation_id,
            lines=lines,
            billing_info=billing_info,
            buyer_vat_context=self._reserve_buyer_vat,
        )

        return tickets, reservation_id

    def _offline_checkout(
        self,
        groups: list[CartGroup],
        seats_per_group: list[list[VenueSeat | None]],
        locked_tiers: list[TicketTier],
        pricings: list[BatchPricing],
        stamps: list[bool],
    ) -> list[Ticket]:
        """Handle offline checkout for batch tickets.

        Creates PENDING tickets that need manual confirmation.

        Args:
            groups: The cart's per-tier groups, in cart order.
            seats_per_group: Each group's resolved seats.
            locked_tiers: Each group's locked tier.
            pricings: Each group's price vector.
            stamps: Whether each group's unit price is written to ``price_paid``.

        Returns:
            List of created PENDING tickets, in cart order.
        """
        tickets: list[Ticket] = []
        for group, seats, locked_tier, pricing, stamp in zip(
            groups, seats_per_group, locked_tiers, pricings, stamps, strict=True
        ):
            tickets.extend(
                self.create_tickets(
                    group.items,
                    seats,
                    Ticket.TicketStatus.PENDING,
                    pricing.lines,
                    tier=locked_tier,
                    discount_code=self._dc_for(locked_tier),
                    stamp_price_paid=stamp,
                )
            )
            self._bump_quantity_sold(locked_tier, len(group.items))

        # Trigger side effects that bulk_create doesn't handle — once for the whole cart
        self.trigger_bulk_create_side_effects(tickets)

        return tickets

    def _at_the_door_checkout(
        self,
        groups: list[CartGroup],
        seats_per_group: list[list[VenueSeat | None]],
        locked_tiers: list[TicketTier],
        pricings: list[BatchPricing],
        stamps: list[bool],
    ) -> list[Ticket]:
        """Handle at-the-door checkout for batch tickets.

        Creates ACTIVE tickets immediately. AT_THE_DOOR represents a commitment
        to attend (pay at arrival), so tickets count toward attendee_count.

        Args:
            groups: The cart's per-tier groups, in cart order.
            seats_per_group: Each group's resolved seats.
            locked_tiers: Each group's locked tier.
            pricings: Each group's price vector.
            stamps: Whether each group's unit price is written to ``price_paid``.

        Returns:
            List of created ACTIVE tickets, in cart order.
        """
        tickets: list[Ticket] = []
        for group, seats, locked_tier, pricing, stamp in zip(
            groups, seats_per_group, locked_tiers, pricings, stamps, strict=True
        ):
            tickets.extend(
                self.create_tickets(
                    group.items,
                    seats,
                    Ticket.TicketStatus.ACTIVE,
                    pricing.lines,
                    tier=locked_tier,
                    discount_code=self._dc_for(locked_tier),
                    stamp_price_paid=stamp,
                )
            )
            self._bump_quantity_sold(locked_tier, len(group.items))

        # Trigger side effects that bulk_create doesn't handle — once for the whole cart
        self.trigger_bulk_create_side_effects(tickets)

        return tickets

    def _free_checkout(
        self,
        groups: list[CartGroup],
        seats_per_group: list[list[VenueSeat | None]],
        locked_tiers: list[TicketTier],
        pricings: list[BatchPricing],
        stamps: list[bool],
    ) -> list[Ticket]:
        """Handle free checkout for batch tickets.

        Creates ACTIVE tickets immediately.

        Nothing is collected on this path — by construction for the FREE **payment
        method**, and by the all-zero price vector for a rerouted ONLINE cart. So what
        gets recorded is ``0.00``, never the vector's list price: a category-priced FREE
        tier carries the seat's price (say 40.00) in ``pricing.lines``, and stamping that
        would report revenue on a giveaway. This mirrors the box-office comp
        (``seating/box_office.py``), the other path that hands out a free seated ticket.

        *Whether* to record is not decided here — ``create_batch`` asks
        ``pricing.should_stamp_price_paid`` once per group and passes the answers, so a
        plain free tier keeps its truthful NULL ("``tier.price`` reconstructs this")
        while a category-priced or buyer-zeroed one — where no tier price reconstructs
        the sale — records the 0.00 (spec §5.5).

        Args:
            groups: The cart's per-tier groups, in cart order.
            seats_per_group: Each group's resolved seats.
            locked_tiers: Each group's locked tier.
            pricings: Each group's price vector (all zero, or a zeroing discount).
            stamps: Whether each group's ``price_paid`` is written at all.

        Returns:
            List of created ACTIVE tickets, in cart order.
        """
        tickets: list[Ticket] = []
        for group, seats, locked_tier, pricing, stamp in zip(
            groups, seats_per_group, locked_tiers, pricings, stamps, strict=True
        ):
            lines = [dataclasses.replace(line, unit_price=ZERO) for line in pricing.lines]
            tickets.extend(
                self.create_tickets(
                    group.items,
                    seats,
                    Ticket.TicketStatus.ACTIVE,
                    lines,
                    tier=locked_tier,
                    discount_code=self._dc_for(locked_tier),
                    stamp_price_paid=stamp,
                )
            )
            self._bump_quantity_sold(locked_tier, len(group.items))

        # Trigger side effects that bulk_create doesn't handle — once for the whole cart
        self.trigger_bulk_create_side_effects(tickets)

        return tickets
