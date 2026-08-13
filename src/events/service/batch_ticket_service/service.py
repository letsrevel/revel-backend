"""Service for batch ticket purchases with seat selection support."""

import typing as t
from decimal import Decimal
from uuid import UUID

import structlog
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import Ticket, TicketTier, VenueSeat
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service.capacity import CapacityMixin
from events.service.batch_ticket_service.checkout import CheckoutMixin
from events.service.batch_ticket_service.context import CartGroup
from events.service.batch_ticket_service.eligibility import PurchaseEligibilityMixin
from events.service.batch_ticket_service.seats import SeatResolutionMixin
from events.service.discount_code_service import assert_min_purchase_amount
from events.service.seating.pricing import (
    ZERO,
    BatchPricing,
    build_batch_pricing,
    cart_is_certainly_free,
    should_stamp_price_paid,
)

if t.TYPE_CHECKING:
    from events.schema.ticket import BuyerBillingInfoSchema

logger = structlog.get_logger(__name__)


class BatchTicketService(PurchaseEligibilityMixin, CapacityMixin, SeatResolutionMixin, CheckoutMixin):
    """Service for creating multiple tickets in a single transaction.

    Handles:
    - Cart-shape validation (one group per tier, uniform currency & payment method)
    - Batch size validation against max_tickets_per_user limits
    - Seat resolution (NONE, BEST_AVAILABLE, USER_CHOICE modes)
    - Atomic ticket creation across every tier in the cart
    - Payment flow delegation (online, offline, free)

    The steps live in the sibling mixins (see the package docstring); this class
    owns only the order they run in.
    """

    def _cart_is_certainly_free(self) -> bool:
        """Whether no ticket in this cart can cost anything — see ``pricing.cart_is_certainly_free``.

        Cart-wide: one paid group is enough to need the buyer's VAT context, so this
        is an ``all()`` over the groups, each asked with its own PWYC amount and its
        own applicable code.

        Returns:
            True when every group is certainly free.
        """
        return all(
            cart_is_certainly_free(group.tier, pwyc_amount=group.pwyc_amount, discount_code=self._dc_for(group.tier))
            for group in self.groups
        )

    def _resolve_single_group(self, items: list[TicketPurchaseItem] | None, pwyc_amount: Decimal | None) -> None:
        """Populate ``self.groups`` from whichever constructor form was used.

        The only place ``self.tier`` may be read: it is the single-tier form's marker,
        and this turns that form into the one-group cart the rest of the engine runs on.

        Args:
            items: ``create_batch``'s ``items`` argument — single-tier form only.
            pwyc_amount: ``create_batch``'s ``pwyc_amount`` argument — single-tier form only.

        Raises:
            TypeError: The single-tier form is missing ``items``, or the cart form was given
                ``items``/``pwyc_amount`` (those belong on each group instead).
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

    def _assert_pwyc_amount(self, group: CartGroup) -> None:
        """Assert this group's PWYC amount is present exactly when its tier is PWYC, and in range.

        Service-authoritative (#846). The controllers check the same thing — the plain
        checkout endpoint refuses a PWYC tier, the PWYC endpoint refuses a non-PWYC one
        and range-checks the amount — but a cart reaches ``create_batch`` through several
        doors, and the amount is now per group rather than per request. The bound
        messages are the controllers' verbatim, so the buyer sees one wording.

        Args:
            group: The cart group to check.

        Raises:
            HttpError: 400 when the amount is missing, forbidden, or out of bounds.
        """
        tier = group.tier
        is_pwyc = tier.price_type == TicketTier.PriceType.PWYC
        if is_pwyc and group.pwyc_amount is None:
            raise HttpError(400, str(_("This tier requires a pay-what-you-can amount.")))
        if not is_pwyc and group.pwyc_amount is not None:
            raise HttpError(400, str(_("This tier does not accept a pay-what-you-can amount.")))
        if group.pwyc_amount is None:
            return
        if group.pwyc_amount < tier.pwyc_min:
            raise HttpError(400, str(_("PWYC amount must be at least {min_amount}")).format(min_amount=tier.pwyc_min))
        if tier.pwyc_max and group.pwyc_amount > tier.pwyc_max:
            raise HttpError(400, str(_("PWYC amount must be at most {max_amount}")).format(max_amount=tier.pwyc_max))

    def _validate_cart(self) -> None:
        """Reject a malformed cart before anything is locked, priced or written.

        Cart *shape* only — whether this buyer may take these tickets (eligibility) and
        whether there is room (capacity) are the next steps' business. Every rule here
        is a whole-cart question that no per-tier step can answer:

        - **One group per tier**, so the per-group loops below can't double-count a tier's
          capacity or write two ``quantity_sold`` increments the caps never saw.
        - **Uniform currency**, because a cart settles as one payment; mixing them would
          need a per-currency split no downstream writer (Payment, invoice) supports.
        - **Uniform payment method**, because the branch dispatch is per cart: an ONLINE
          + FREE mix would have to be two checkouts.
        - **PWYC per group** — see :meth:`_assert_pwyc_amount`.
        - **No seat twice.** Two groups naming the same seat each pass their own
          USER_CHOICE validation (it matches distinct ids against the shared lock) and
          collide only at the ``unique_ticket_event_seat`` constraint — a 500 where the
          buyer deserves a 400. Payload-level, so it catches a repeat within one group too.

        Raises:
            HttpError: 400 for a duplicated tier, mixed currency, mixed payment method, a
                missing/forbidden/out-of-bounds PWYC amount, or a seat requested twice.
        """
        tier_ids = [group.tier.pk for group in self.groups]
        if len(set(tier_ids)) != len(tier_ids):
            raise HttpError(400, str(_("Each tier may appear only once per checkout.")))

        if len({group.tier.currency for group in self.groups}) > 1:
            raise HttpError(400, str(_("All tickets in one checkout must use the same currency.")))

        if len({group.tier.payment_method for group in self.groups}) > 1:
            raise HttpError(400, str(_("All tickets in one checkout must use the same payment method.")))

        for group in self.groups:
            self._assert_pwyc_amount(group)

        seat_ids = [item.seat_id for group in self.groups for item in group.items if item.seat_id is not None]
        if len(set(seat_ids)) != len(seat_ids):
            raise HttpError(400, str(_("The same seat cannot be purchased twice.")))

    def _price_cart(
        self, locked_tiers: list[TicketTier], seats_per_group: list[list[VenueSeat | None]]
    ) -> tuple[list[BatchPricing], list[bool]]:
        """Price every ticket in every group, and decide per group whether it stamps.

        Single source of truth for PWYC *and* discounts, and the only place that reads a
        tier's category map. Priced off the LOCKED tier, so a concurrent repricing can't
        be undercut by a stale pre-lock read.

        Also the one place ``min_purchase_amount`` is enforced — not in
        ``validate_discount_code``, because only now is the cart's real total known
        (spec §5.6). It is measured over the groups the code actually applies to: a
        threshold met by tiers the code cannot discount would be met by a cart it takes
        nothing off.

        Args:
            locked_tiers: Each group's locked tier, in cart order.
            seats_per_group: Each group's resolved seats, in cart order.

        Returns:
            Each group's price vector, and whether each group writes ``price_paid``.

        Raises:
            HttpError: 400 if the discountable total is below the code's minimum, or if
                a seat is in a category the tier does not price.
        """
        pricings: list[BatchPricing] = []
        stamps: list[bool] = []
        for group, locked_tier, seats in zip(self.groups, locked_tiers, seats_per_group, strict=True):
            group_code = self._dc_for(locked_tier)
            pricings.append(
                build_batch_pricing(locked_tier, seats, pwyc_amount=group.pwyc_amount, discount_code=group_code)
            )
            # One authority for every writer (spec §5.5), per group. Computed ONCE and
            # handed to every branch that stamps — a branch that recomputes, or silently
            # drops it, is exactly how a category-priced tier ended up with NULL
            # price_paid rows. The ONLINE branch is the sole exception and does not take
            # it at all — a PERMANENT carve-out (#758): Payment.amount is authoritative
            # there (and is *net* for a reverse-charge buyer). An ONLINE cart the buyer
            # zeroed has no Payment row, so it reroutes to free and does stamp.
            stamps.append(
                should_stamp_price_paid(locked_tier, pwyc_amount=group.pwyc_amount, has_discount=group_code is not None)
            )

        if self.discount_code is not None:
            discountable_total = sum(
                (
                    pricing.gross_total
                    for locked_tier, pricing in zip(locked_tiers, pricings, strict=True)
                    if self._dc_for(locked_tier) is not None
                ),
                ZERO,
            )
            assert_min_purchase_amount(self.discount_code, discountable_total)

        return pricings, stamps

    def _reroutes_to_free(self, payment_method: str, pricings: list[BatchPricing]) -> bool:
        """Does the buyer's own input turn this ONLINE cart into a free one?

        All-or-nothing over the WHOLE cart: a cart mixing 0.00 and positive units stays
        on the paid path so each ticket keeps its 1:1 Payment row (the refund matcher
        relies on that pairing). A zero-priced ONLINE tier with no PWYC/discount input is
        still a misconfiguration, not a free tier — it keeps falling through to the 400
        in ``reserve_batch_payments``.

        Args:
            payment_method: The cart's (uniform, locked) payment method.
            pricings: Each group's price vector.

        Returns:
            True when every line of every group is zero because the buyer moved the price.
        """
        if payment_method != TicketTier.PaymentMethod.ONLINE:
            return False
        buyer_reduced_price = self.discount_code is not None or any(
            group.pwyc_amount is not None for group in self.groups
        )
        all_lines = [line for pricing in pricings for line in pricing.lines]
        return buyer_reduced_price and bool(all_lines) and all(line.unit_price <= 0 for line in all_lines)

    @transaction.atomic
    def create_batch(
        self,
        items: list[TicketPurchaseItem] | None = None,
        pwyc_amount: Decimal | None = None,
        billing_info: "BuyerBillingInfoSchema | None" = None,
    ) -> list[Ticket] | tuple[list[Ticket], UUID]:
        """Create a batch of tickets, spanning as many tiers as the cart holds.

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
            Tickets come back in cart order — every group's, concatenated.

        Raises:
            TypeError: If the single-tier form is missing ``items``, or the cart
                form is given ``items``/``pwyc_amount``.
            HttpError: If validation fails or ticket creation fails.
            UserIsIneligibleError: If a tier is gated to membership tiers the buyer
                does not hold.
        """
        self._resolve_single_group(items, pwyc_amount)

        # Whole-cart shape first: cheapest checks, and the per-tier loops below assume
        # a tier appears once and the cart settles in one currency/method.
        self._validate_cart()

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
        self.assert_ticket_names([item for group in self.groups for item in group.items])

        # The cart settles as one payment, so the method is a cart-level fact —
        # _validate_cart just proved every group agrees on it.
        payment_method = self.groups[0].tier.payment_method
        total_count = sum(len(group.items) for group in self.groups)

        # Resolve the buyer's VAT context (incl. the VIES round-trip) BEFORE
        # locking the tiers, so a contended row is never held across VIES
        # (#632). Price-independent: the arithmetic runs post-lock against each
        # locked tier's fresh price. Only the paid-online path creates Stripe
        # Payment rows; other methods skip it.
        buyer_vat = None
        if payment_method == TicketTier.PaymentMethod.ONLINE and not self._cart_is_certainly_free():
            from events.service import stripe_service

            buyer_vat = stripe_service.resolve_attendee_vat_for_reserve(billing_info=billing_info)
        self._reserve_buyer_vat = buyer_vat  # consumed by _online_checkout

        # Lock every tier in the cart for the capacity checks, in PK order — the
        # deadlock discipline two concurrent carts naming the same tiers in opposite
        # order depend on (same protocol as series_pass_purchase). One query, so the
        # lock set is taken atomically rather than group by group.
        locked = {
            tier.pk: tier
            for tier in TicketTier.objects.select_for_update()
            .filter(pk__in=[group.tier.pk for group in self.groups])
            .order_by("pk")
        }
        locked_tiers = [locked[group.tier.pk] for group in self.groups]
        # Dispatch off the LOCKED rows, as the single-tier engine always did — the
        # pre-lock read above only decides whether VIES is worth a round-trip.
        locked_payment_method = locked_tiers[0].payment_method

        # Check each tier's own capacity against its own group...
        for group, locked_tier in zip(self.groups, locked_tiers, strict=True):
            self.assert_tier_capacity(locked_tier, len(group.items))

        # ...the event's (effective_capacity - soft limit) against the WHOLE cart —
        # per-group would let 2 + 2 through a remaining capacity of 3...
        self.assert_event_capacity(total_count)

        # ...and each sector's (hard limit - cannot be overridden) against the summed
        # demand of every GA group that touches it.
        self.assert_sector_capacities(self.groups)

        # Resolve seats for every group in one cross-group pass (single PK-ordered
        # lock over the cart's USER_CHOICE seats — see seats.py).
        seats_per_group = self.resolve_cart_seats(self.groups)

        pricings, stamps = self._price_cart(locked_tiers, seats_per_group)

        # Log the batch purchase attempt for audit trail
        logger.info(
            "batch_ticket_purchase_started",
            user_id=str(self.user.id),
            event_id=str(self.event.id),
            tier_ids=[str(tier.id) for tier in locked_tiers],
            ticket_count=total_count,
            payment_method=locked_payment_method,
            seat_assignment_modes=[tier.seat_assignment_mode for tier in locked_tiers],
            has_seats=any(seat is not None for seats in seats_per_group for seat in seats),
        )

        # If the buyer's input drives EVERY ticket in EVERY group to zero, an ONLINE
        # cart becomes a free checkout. All-or-nothing over the whole cart: a cart
        # mixing 0.00 and positive units stays on the paid path so each ticket keeps
        # its 1:1 Payment row (the refund matcher relies on that pairing). A
        # zero-priced ONLINE tier with no PWYC/discount input is still a
        # misconfiguration, not a free tier — it keeps falling through to the 400 in
        # reserve_batch_payments.
        #
        # ``stamps`` is carried into the reroute: getting here means the buyer moved the
        # price, so every flag is True, and there is no Payment row to hold the amount
        # instead. Dropping it left ``price_paid`` NULL — the positive claim that
        # ``tier.price`` reconstructs the sale — on a ticket that cost 0.00 (spec §5.5).
        # Still threaded per group rather than collapsed: the flag is a per-tier fact.
        result: list[Ticket] | tuple[list[Ticket], UUID]
        if self._reroutes_to_free(locked_payment_method, pricings):
            result = self._free_checkout(self.groups, seats_per_group, locked_tiers, pricings, stamps)
        else:
            # Delegate to payment-specific method
            match locked_payment_method:
                case TicketTier.PaymentMethod.ONLINE:
                    result = self._online_checkout(self.groups, seats_per_group, locked_tiers, pricings, billing_info)
                case TicketTier.PaymentMethod.OFFLINE:
                    result = self._offline_checkout(self.groups, seats_per_group, locked_tiers, pricings, stamps)
                case TicketTier.PaymentMethod.AT_THE_DOOR:
                    result = self._at_the_door_checkout(self.groups, seats_per_group, locked_tiers, pricings, stamps)
                case TicketTier.PaymentMethod.FREE:
                    result = self._free_checkout(self.groups, seats_per_group, locked_tiers, pricings, stamps)
                case _:
                    raise HttpError(400, str(_("Unknown payment method.")))

        # Cart-level effects, applied once after every group has been written —
        # regardless of which branch above wrote them.
        created_tickets = result[0] if isinstance(result, tuple) else result
        if self.discount_code is not None:
            from events.service import discount_code_service

            discount_code_service.apply_discount(self.discount_code, self.user, len(created_tickets))
        self._claim_waitlist_offer_if_any()

        return result
