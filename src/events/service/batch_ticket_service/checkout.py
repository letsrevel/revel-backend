"""One method per payment method — what happens once the cart is priced.

Each ``_*_checkout`` receives an already validated, seated and priced cart and
decides only three things: the status the tickets get, whether Payment rows are
created, and which side effects fire. ``create_batch`` picks the method.

Every branch takes the cart as one :class:`ResolvedGroup` per group, writes each
group's tickets against its own locked tier, and moves each tier's
``quantity_sold`` by that group's count alone.
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


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedGroup:
    """One cart group after locking, seating and pricing — what checkout consumes.

    ``_price_cart`` builds one per group, in cart order, so the per-group facts
    travel together instead of as positionally-aligned parallel lists.
    """

    group: CartGroup
    seats: list[VenueSeat | None]
    locked_tier: TicketTier
    pricing: BatchPricing
    stamp: bool
    """Whether this group's unit price is written to ``price_paid``. The ONLINE
    branch never reads it — the PERMANENT #758 carve-out (Payment.amount is
    authoritative there)."""


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
        resolved: list[ResolvedGroup],
        billing_info: "BuyerBillingInfoSchema | None" = None,
    ) -> tuple[list[Ticket], UUID]:
        """Reserve an online batch: PENDING tickets + PENDING Payment rows (#632).

        Does NOT call Stripe — the caller returns the reservation_id to the
        client, which then calls the checkout-session endpoint. Keeping Stripe
        out of this method is what lets the request commit and release the tier
        locks before the ~2.5s Session.create round-trip. Attendee VAT (VIES) was
        already resolved before the lock in create_batch and is passed through.

        Args:
            resolved: The cart's resolved groups, in cart order. The only branch
                that ignores ``ResolvedGroup.stamp`` — see the field's docstring.
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
        for rg in resolved:
            # PENDING tickets; price_paid stays NULL online — PERMANENTLY (#758). Payment.amount is
            # authoritative (spec §5.5) and is net for a reverse-charge buyer, so stamping it would
            # make price_paid's meaning depend on the buyer's VAT status. Never pass stamp_price_paid.
            tickets.extend(
                self.create_tickets(
                    rg.group.items,
                    rg.seats,
                    Ticket.TicketStatus.PENDING,
                    rg.pricing.lines,
                    tier=rg.locked_tier,
                    discount_code=self._dc_for(rg.locked_tier),
                )
            )
            lines.extend(rg.pricing.lines)
            self._bump_quantity_sold(rg.locked_tier, len(rg.group.items))

        # Create PENDING Payment rows for the reservation (no Stripe call). One
        # reservation covers the whole cart, so the tickets and lines go in flattened
        # in cart order — they stay positionally aligned. reserve_batch_payments prices
        # and VATs each ticket off its OWN tier (ticket.tier), so a multi-tier ONLINE
        # cart bills every group at its own tier's price and VAT rate (#846).
        stripe_service.reserve_batch_payments(
            event=self.event,
            user=self.user,
            tickets=tickets,
            reservation_id=reservation_id,
            lines=lines,
            billing_info=billing_info,
            buyer_vat_context=self._reserve_buyer_vat,
        )

        return tickets, reservation_id

    def _offline_checkout(self, resolved: list[ResolvedGroup]) -> list[Ticket]:
        """Handle offline checkout for batch tickets.

        Creates PENDING tickets that need manual confirmation.

        Args:
            resolved: The cart's resolved groups, in cart order.

        Returns:
            List of created PENDING tickets, in cart order.
        """
        tickets: list[Ticket] = []
        for rg in resolved:
            tickets.extend(
                self.create_tickets(
                    rg.group.items,
                    rg.seats,
                    Ticket.TicketStatus.PENDING,
                    rg.pricing.lines,
                    tier=rg.locked_tier,
                    discount_code=self._dc_for(rg.locked_tier),
                    stamp_price_paid=rg.stamp,
                )
            )
            self._bump_quantity_sold(rg.locked_tier, len(rg.group.items))

        # Trigger side effects that bulk_create doesn't handle — once for the whole cart
        self.trigger_bulk_create_side_effects(tickets)

        return tickets

    def _at_the_door_checkout(self, resolved: list[ResolvedGroup]) -> list[Ticket]:
        """Handle at-the-door checkout for batch tickets.

        Creates ACTIVE tickets immediately. AT_THE_DOOR represents a commitment
        to attend (pay at arrival), so tickets count toward attendee_count.

        Args:
            resolved: The cart's resolved groups, in cart order.

        Returns:
            List of created ACTIVE tickets, in cart order.
        """
        tickets: list[Ticket] = []
        for rg in resolved:
            tickets.extend(
                self.create_tickets(
                    rg.group.items,
                    rg.seats,
                    Ticket.TicketStatus.ACTIVE,
                    rg.pricing.lines,
                    tier=rg.locked_tier,
                    discount_code=self._dc_for(rg.locked_tier),
                    stamp_price_paid=rg.stamp,
                )
            )
            self._bump_quantity_sold(rg.locked_tier, len(rg.group.items))

        # Trigger side effects that bulk_create doesn't handle — once for the whole cart
        self.trigger_bulk_create_side_effects(tickets)

        return tickets

    def _free_checkout(self, resolved: list[ResolvedGroup]) -> list[Ticket]:
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
            resolved: The cart's resolved groups, in cart order (price vectors all
                zero, or zeroed by a discount).

        Returns:
            List of created ACTIVE tickets, in cart order.
        """
        tickets: list[Ticket] = []
        for rg in resolved:
            lines = [dataclasses.replace(line, unit_price=ZERO) for line in rg.pricing.lines]
            tickets.extend(
                self.create_tickets(
                    rg.group.items,
                    rg.seats,
                    Ticket.TicketStatus.ACTIVE,
                    lines,
                    tier=rg.locked_tier,
                    discount_code=self._dc_for(rg.locked_tier),
                    stamp_price_paid=rg.stamp,
                )
            )
            self._bump_quantity_sold(rg.locked_tier, len(rg.group.items))

        # Trigger side effects that bulk_create doesn't handle — once for the whole cart
        self.trigger_bulk_create_side_effects(tickets)

        return tickets
