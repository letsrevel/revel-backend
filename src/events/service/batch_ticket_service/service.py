"""Service for batch ticket purchases with seat selection support."""

import dataclasses
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
from events.service.batch_ticket_service.checkout import CheckoutMixin, ResolvedGroup
from events.service.batch_ticket_service.context import CartGroup, assert_uniform_cart, validate_cart_shape
from events.service.batch_ticket_service.eligibility import PurchaseEligibilityMixin
from events.service.batch_ticket_service.seats import SeatResolutionMixin
from events.service.discount_code_service import assert_min_purchase_amount
from events.service.seating.pricing import (
    ZERO,
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

    def _validate_cart(self) -> None:
        """Reject a malformed cart before anything is locked, priced or written.

        Delegates to :func:`~events.service.batch_ticket_service.context.validate_cart_shape`
        (#846 review fix) — the single authority for cart-shape validation, shared
        with the guest checkout's pre-branch so the two can never drift.

        Raises:
            HttpError: 400 for a duplicated tier, mixed currency, mixed payment method, a
                missing/forbidden/out-of-bounds PWYC amount, or a seat requested twice.
        """
        validate_cart_shape(self.groups)

    @staticmethod
    def _assert_locked_cart_uniformity(locked_tiers: list[TicketTier]) -> None:
        """Re-assert the cart's uniformity invariants on the LOCKED tier rows.

        ``_validate_cart`` proves uniform currency and payment method on the
        *pre-lock* instances the controller loaded, and an organizer write can
        commit between that read and the ``select_for_update`` below (the VIES
        round-trip deliberately sits in that window). Every money decision that
        follows — the branch dispatch, the Payment rows' currency, the Stripe
        line items — is a cart-level read off ONE tier, so it must be proven on
        the locked rows: a sibling flipped from FREE to ONLINE would otherwise
        ride the free path and have its revenue zeroed, and a flipped currency
        would be charged and recorded in the wrong one. Same rules, same
        messages as ``validate_cart_shape`` — this is that check, re-run on the
        rows the writes are made against — delegates to
        :func:`~events.service.batch_ticket_service.context.assert_uniform_cart`,
        the same authority ``validate_cart_shape`` uses, so the two can never drift.

        Args:
            locked_tiers: Each group's locked tier, in cart order.

        Raises:
            HttpError: 400 if the locked rows disagree on currency or payment method.
        """
        assert_uniform_cart(locked_tiers)

    def _price_cart(
        self, locked_tiers: list[TicketTier], seats_per_group: list[list[VenueSeat | None]]
    ) -> list[ResolvedGroup]:
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
            One :class:`ResolvedGroup` per group, in cart order — the group with its
            seats, locked tier, price vector and ``price_paid`` stamp flag.

        Raises:
            HttpError: 400 if the discountable total is below the code's minimum, or if
                a seat is in a category the tier does not price.
        """
        resolved: list[ResolvedGroup] = []
        for group, locked_tier, seats in zip(self.groups, locked_tiers, seats_per_group, strict=True):
            group_code = self._dc_for(locked_tier)
            # stamp: one authority for every writer (spec §5.5), per group. Computed ONCE
            # and handed to every branch that stamps — a branch that recomputes, or
            # silently drops it, is exactly how a category-priced tier ended up with NULL
            # price_paid rows. The ONLINE branch is the sole exception and never reads
            # it — a PERMANENT carve-out (#758): Payment.amount is authoritative there
            # (and is *net* for a reverse-charge buyer). An ONLINE cart the buyer zeroed
            # has no Payment row, so it reroutes to free and does stamp.
            resolved.append(
                ResolvedGroup(
                    group=group,
                    seats=seats,
                    locked_tier=locked_tier,
                    pricing=build_batch_pricing(
                        locked_tier, seats, pwyc_amount=group.pwyc_amount, discount_code=group_code
                    ),
                    stamp=should_stamp_price_paid(
                        locked_tier, pwyc_amount=group.pwyc_amount, has_discount=group_code is not None
                    ),
                )
            )

        if self.discount_code is not None:
            discountable_total = sum(
                (rg.pricing.gross_total for rg in resolved if self._dc_for(rg.locked_tier) is not None),
                ZERO,
            )
            assert_min_purchase_amount(self.discount_code, discountable_total)

        return resolved

    def _reroutes_to_free(self, payment_method: str, resolved: list[ResolvedGroup]) -> bool:
        """Does the buyer's own input turn this ONLINE cart into a free one?

        All-or-nothing over the WHOLE cart: a cart mixing 0.00 and positive units stays
        on the paid path so each ticket keeps its 1:1 Payment row (the refund matcher
        relies on that pairing). A zero-priced ONLINE tier with no PWYC/discount input is
        still a misconfiguration, not a free tier — it keeps falling through to the 400
        in ``reserve_batch_payments``. The buyer-input test is cart-level, not per-group:
        a discount scoped to one tier (or one group's PWYC amount) can vouch for a
        DIFFERENT group's 0.00 ONLINE line, letting a misconfigured tier ride a reduced
        cart onto the free path instead of that 400. Tolerated: it needs an organizer to
        have created a 0.00 ONLINE tier (blocked by tier validation) AND every line of
        the cart to be zero.

        Args:
            payment_method: The cart's (uniform, locked) payment method.
            resolved: The cart's resolved groups.

        Returns:
            True when every line of every group is zero because the buyer moved the price.
        """
        if payment_method != TicketTier.PaymentMethod.ONLINE:
            return False
        buyer_reduced_price = self.discount_code is not None or any(
            group.pwyc_amount is not None for group in self.groups
        )
        all_lines = [line for rg in resolved for line in rg.pricing.lines]
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
        # Organizer writes can land between the pre-lock read and the lock, so the
        # uniformity _validate_cart proved on the stale instances is re-proven here:
        # every money decision below is a cart-level read off ONE locked tier.
        self._assert_locked_cart_uniformity(locked_tiers)

        # Per-user ticket caps, layered across the whole cart (#846 Decision 4). Runs
        # HERE, under the tier locks (and taking the Event row lock for the event-wide
        # cap), so two concurrent carts by the same buyer are serialized and cannot both
        # read a stale pre-write count and slip past max_tickets_per_user (TOCTOU). The
        # groups are rebound to the LOCKED tier rows so the tier caps are read fresh too —
        # a pre-lock instance could still say None for a cap the organizer just enabled.
        locked_groups = [
            dataclasses.replace(group, tier=locked_tier)
            for group, locked_tier in zip(self.groups, locked_tiers, strict=True)
        ]
        self.assert_per_user_limits(locked_groups)

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

        resolved = self._price_cart(locked_tiers, seats_per_group)

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
        # ``ResolvedGroup.stamp`` is carried into the reroute: there is no Payment row here
        # to hold the amount instead, so a group the buyer DID move must record its 0.00 —
        # dropping the flag left ``price_paid`` NULL, the positive claim that ``tier.price``
        # reconstructs the sale, on a ticket that cost 0.00 (spec §5.5). Threaded per group
        # rather than forced True across the cart: the reroute is a cart-level decision, but
        # the flag is a per-tier fact, and a group with no PWYC/discount input on a genuinely
        # 0.00 tier correctly keeps its NULL.
        result: list[Ticket] | tuple[list[Ticket], UUID]
        if self._reroutes_to_free(locked_payment_method, resolved):
            result = self._free_checkout(resolved)
        else:
            # Delegate to payment-specific method
            match locked_payment_method:
                case TicketTier.PaymentMethod.ONLINE:
                    result = self._online_checkout(resolved, billing_info)
                case TicketTier.PaymentMethod.OFFLINE:
                    result = self._offline_checkout(resolved)
                case TicketTier.PaymentMethod.AT_THE_DOOR:
                    result = self._at_the_door_checkout(resolved)
                case TicketTier.PaymentMethod.FREE:
                    result = self._free_checkout(resolved)
                case _:
                    raise HttpError(400, str(_("Unknown payment method.")))

        # Cart-level effects, applied once after every group has been written —
        # regardless of which branch above wrote them.
        created_tickets = result[0] if isinstance(result, tuple) else result
        if self.discount_code is not None:
            from events.service import discount_code_service

            # Count the tickets that actually CARRY the code, not the cart. apply_discount
            # both re-checks ``times_used + batch_size > max_uses`` under lock and adds
            # ``batch_size`` to ``times_used``, so charging a scoped code for the groups it
            # doesn't price would burn uses that were never granted — and could refuse a
            # legitimate cart with a 400 on a limit it hasn't actually reached.
            discounted_count = len([ticket for ticket in created_tickets if ticket.discount_code_id is not None])
            discount_code_service.apply_discount(self.discount_code, self.user, discounted_count)
        self.claim_waitlist_offer_if_any()

        return result
