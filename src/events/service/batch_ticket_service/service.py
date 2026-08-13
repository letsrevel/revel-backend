"""Service for batch ticket purchases with seat selection support."""

import typing as t
from decimal import Decimal
from uuid import UUID

import structlog
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import Ticket, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service.capacity import CapacityMixin
from events.service.batch_ticket_service.checkout import CheckoutMixin
from events.service.batch_ticket_service.context import CartGroup
from events.service.batch_ticket_service.eligibility import PurchaseEligibilityMixin
from events.service.batch_ticket_service.seats import SeatResolutionMixin
from events.service.discount_code_service import assert_min_purchase_amount
from events.service.seating.pricing import build_batch_pricing, cart_is_certainly_free, should_stamp_price_paid

if t.TYPE_CHECKING:
    from events.schema.ticket import BuyerBillingInfoSchema

logger = structlog.get_logger(__name__)


class BatchTicketService(PurchaseEligibilityMixin, CapacityMixin, SeatResolutionMixin, CheckoutMixin):
    """Service for creating multiple tickets in a single transaction.

    Handles:
    - Batch size validation against max_tickets_per_user limits
    - Seat resolution (NONE, BEST_AVAILABLE, USER_CHOICE modes)
    - Atomic ticket creation
    - Payment flow delegation (online, offline, free)

    The steps live in the sibling mixins (see the package docstring); this class
    owns only the order they run in.
    """

    def _cart_is_certainly_free(self, pwyc_amount: Decimal | None) -> bool:
        """Whether no ticket in this cart can cost anything — see ``pricing.cart_is_certainly_free``."""
        return cart_is_certainly_free(self.tier, pwyc_amount=pwyc_amount, discount_code=self.discount_code)

    def _resolve_single_group(
        self, items: list[TicketPurchaseItem] | None, pwyc_amount: Decimal | None
    ) -> tuple[list[TicketPurchaseItem], Decimal | None]:
        """Populate ``self.groups`` from whichever constructor form was used and return the one group's cart.

        Args:
            items: ``create_batch``'s ``items`` argument — single-tier form only.
            pwyc_amount: ``create_batch``'s ``pwyc_amount`` argument — single-tier form only.

        Returns:
            The sole group's ``(items, pwyc_amount)``, for the rest of ``create_batch`` to read.

        Raises:
            TypeError: The single-tier form is missing ``items``, or the cart form was given
                ``items``/``pwyc_amount`` (those belong on each group instead).
            NotImplementedError: The cart holds more than one group — lifted by the
                multi-group cart engine task.
        """
        if self._single_tier_form:
            if items is None:
                raise TypeError("items is required in the single-tier form")
            self.groups = [
                CartGroup(
                    tier=self.tier,
                    items=items,
                    pwyc_amount=pwyc_amount,
                    price_category_id=self.price_category_id,
                    accessible_required=self.accessible_required,
                )
            ]
        elif items is not None or pwyc_amount is not None:
            raise TypeError("items/pwyc_amount belong to the groups in the cart form")
        if len(self.groups) > 1:
            raise NotImplementedError("multi-group carts are not implemented yet")  # lifted by the cart-engine task
        return self.groups[0].items, self.groups[0].pwyc_amount

    @transaction.atomic
    def create_batch(
        self,
        items: list[TicketPurchaseItem] | None = None,
        pwyc_amount: Decimal | None = None,
        billing_info: "BuyerBillingInfoSchema | None" = None,
    ) -> list[Ticket] | tuple[list[Ticket], UUID]:
        """Create a batch of tickets.

        For online payment tiers, reserves the batch (PENDING tickets + PENDING
        Payment rows) and returns the tickets with a reservation_id.
        For free/offline/at-the-door tiers, returns the created tickets.

        Args:
            items: List of ticket purchase items with guest_name and optional seat_id.
                Single-tier form only — required there, forbidden in the cart form
                (where each :class:`~events.service.batch_ticket_service.context.CartGroup`
                carries its own items).
            pwyc_amount: The buyer's pay-what-you-can amount. **PWYC only** — a
                discount is no longer pre-computed into this parameter by callers;
                pass the validated code as ``discount_code`` to the constructor and
                the pricing service applies it per ticket. Single-tier form only.
            billing_info: Optional buyer billing info for attendee invoicing.

        Returns:
            Either a `(tickets, reservation_id)` tuple for the ONLINE payment
            method, or a list of created Tickets for free/offline/at-the-door.

        Raises:
            TypeError: If the single-tier form is missing ``items``, or the cart
                form is given ``items``/``pwyc_amount``.
            NotImplementedError: If the cart holds more than one group — lifted by
                the multi-group cart engine task.
            HttpError: If validation fails or ticket creation fails.
            UserIsIneligibleError: If the tier is gated to membership tiers the buyer
                does not hold.
        """
        items, pwyc_amount = self._resolve_single_group(items, pwyc_amount)

        # Per-tier eligibility: purchasability (invitation-linked restrictions,
        # membership), the membership *tier* the organizer gated this tier to (#807,
        # runs second so the coarser purchasable_by 403 still answers first), and the
        # sale window (#846 — previously never enforced on this path at all).
        for group in self.groups:
            self._assert_purchasable_by(group.tier)
            self._assert_membership_tier_allowed(group.tier)
            self._assert_sale_window(group.tier)
        # Per-user ticket caps, layered across the whole cart (#846 Decision 4).
        self.assert_per_user_limits(self.groups)

        # Names are enforced here — not in the schema — because only the event knows
        # whether it requires them (#845).
        self.assert_ticket_names([item for g in self.groups for item in g.items])

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
        self.assert_sector_capacities(self.groups)

        # Resolve seats. Single-group form only for now — Task 7's cart engine
        # consumes the full per-group list.
        seats_per_group = self.resolve_cart_seats(self.groups)
        seats = seats_per_group[0]

        # Price every ticket. Single source of truth for PWYC *and* discounts, and
        # the only place that reads the tier's category map. Priced off the LOCKED
        # tier, so a concurrent repricing can't be undercut by a stale pre-lock read.
        pricing = build_batch_pricing(locked_tier, seats, pwyc_amount=pwyc_amount, discount_code=self.discount_code)
        # min_purchase_amount is enforced HERE, not in validate_discount_code: only now
        # is the cart's real total known (spec §5.6).
        if self.discount_code is not None:
            assert_min_purchase_amount(self.discount_code, pricing.gross_total)
        # One authority for every writer (spec §5.5). Computed ONCE and handed to every
        # branch that stamps — a branch that recomputes, or silently drops it, is exactly
        # how a category-priced tier ended up with NULL price_paid rows. The ONLINE branch
        # is the sole exception and does not take it at all — a PERMANENT carve-out (#758):
        # Payment.amount is authoritative there (and is *net* for a reverse-charge buyer).
        # An ONLINE cart the buyer zeroed has no Payment row, so it reroutes to free and
        # does stamp.
        stamp_price_paid = should_stamp_price_paid(
            locked_tier, pwyc_amount=pwyc_amount, has_discount=self.discount_code is not None
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
        #
        # ``stamp_price_paid`` is carried into the reroute: getting here means the buyer
        # moved the price, so it is always True, and there is no Payment row to hold the
        # amount instead. Dropping it left ``price_paid`` NULL — the positive claim that
        # ``tier.price`` reconstructs the sale — on a ticket that cost 0.00 (spec §5.5).
        buyer_reduced_price = pwyc_amount is not None or self.discount_code is not None
        result: list[Ticket] | tuple[list[Ticket], UUID]
        if (
            locked_tier.payment_method == TicketTier.PaymentMethod.ONLINE
            and buyer_reduced_price
            and pricing.lines
            and all(line.unit_price <= 0 for line in pricing.lines)
        ):
            result = self._free_checkout(items, seats, locked_tier, pricing, stamp_price_paid)
        else:
            # Delegate to payment-specific method
            match locked_tier.payment_method:
                case TicketTier.PaymentMethod.ONLINE:
                    result = self._online_checkout(items, seats, locked_tier, pricing, billing_info)
                case TicketTier.PaymentMethod.OFFLINE:
                    result = self._offline_checkout(items, seats, locked_tier, pricing, stamp_price_paid)
                case TicketTier.PaymentMethod.AT_THE_DOOR:
                    result = self._at_the_door_checkout(items, seats, locked_tier, pricing, stamp_price_paid)
                case TicketTier.PaymentMethod.FREE:
                    result = self._free_checkout(items, seats, locked_tier, pricing, stamp_price_paid)
                case _:
                    raise HttpError(400, str(_("Unknown payment method.")))

        # Cart-level effects, applied once regardless of which branch above wrote the
        # tickets. Single-group today (identical count/order to the pre-hoist behavior);
        # Task 7's cart engine will call these once after every group has been written.
        created_tickets = result[0] if isinstance(result, tuple) else result
        if self.discount_code is not None:
            from events.service import discount_code_service

            discount_code_service.apply_discount(self.discount_code, self.user, len(created_tickets))
        self._claim_waitlist_offer_if_any()

        return result
