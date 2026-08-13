"""Request-scoped state shared by every ``BatchTicketService`` mixin."""

import dataclasses
import typing as t
from decimal import Decimal
from uuid import UUID

from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, TicketTier
from events.models.discount_code import DiscountCode
from events.schema import TicketPurchaseItem

if t.TYPE_CHECKING:
    from events.service.attendee_vat_service import BuyerVATContext


@dataclasses.dataclass
class CartGroup:
    """One tier's slice of a checkout cart — the granularity every per-tier step runs at."""

    tier: TicketTier
    items: list[TicketPurchaseItem]
    pwyc_amount: Decimal | None = None
    price_category_id: UUID | None = None
    accessible_required: bool = False


def assert_pwyc_amount(group: CartGroup) -> None:
    """Assert this group's PWYC amount is present exactly when its tier is PWYC, and in range.

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


def validate_cart_shape(groups: list[CartGroup]) -> None:
    """Reject a malformed cart before anything is locked, priced or written.

    The single authority for cart-*shape* validation (#846 review fix): both
    ``BatchTicketService._validate_cart`` and the guest checkout's pre-branch
    (``events.service.guest.handle_guest_ticket_checkout``, which must 400 a
    malformed non-online cart *before* sending a confirmation email — see its
    call site) run this exact function, so the two can never drift.

    Cart *shape* only — whether this buyer may take these tickets (eligibility) and
    whether there is room (capacity) are the caller's business. Every rule here is a
    whole-cart question that no per-tier step can answer:

    - **A non-empty cart**, because every rule below and every downstream branch
      (payment method, currency) reads ``groups[0]`` or reduces over the groups.
    - **One group per tier**, so the per-group loops elsewhere can't double-count a
      tier's capacity or write two ``quantity_sold`` increments the caps never saw.
    - **Uniform currency**, because a cart settles as one payment; mixing them would
      need a per-currency split no downstream writer (Payment, invoice) supports.
    - **Uniform payment method**, because the branch dispatch is per cart: an ONLINE
      + FREE mix would have to be two checkouts.
    - **PWYC per group** — see :func:`assert_pwyc_amount`.
    - **No seat twice.** Two groups naming the same seat each pass their own
      USER_CHOICE validation (it matches distinct ids against the shared lock) and
      collide only at the ``unique_ticket_event_seat`` constraint — a 500 where the
      buyer deserves a 400. Payload-level, so it catches a repeat within one group too.

    Args:
        groups: The cart's groups, in cart order.

    Raises:
        HttpError: 400 for an empty cart, a duplicated tier, mixed currency, mixed
            payment method, a missing/forbidden/out-of-bounds PWYC amount, or a seat
            requested twice.
    """
    if not groups:
        raise HttpError(400, str(_("Your cart is empty.")))

    tier_ids = [group.tier.pk for group in groups]
    if len(set(tier_ids)) != len(tier_ids):
        raise HttpError(400, str(_("Each tier may appear only once per checkout.")))

    if len({group.tier.currency for group in groups}) > 1:
        raise HttpError(400, str(_("All tickets in one checkout must use the same currency.")))

    if len({group.tier.payment_method for group in groups}) > 1:
        raise HttpError(400, str(_("All tickets in one checkout must use the same payment method.")))

    for group in groups:
        assert_pwyc_amount(group)

    seat_ids = [item.seat_id for group in groups for item in group.items if item.seat_id is not None]
    if len(set(seat_ids)) != len(seat_ids):
        raise HttpError(400, str(_("The same seat cannot be purchased twice.")))


class BatchTicketContext:
    """The inputs of a single batch purchase.

    Every mixin in this package inherits from this class so ``self.event``,
    ``self.groups`` & co. are declared exactly once and type-check under
    ``mypy --strict``. It is never instantiated directly — see
    :class:`~events.service.batch_ticket_service.service.BatchTicketService`.

    Supports two construction forms:

    - **Single-tier form** (legacy): pass ``tier``; the single :class:`CartGroup`
        is assembled in ``create_batch`` once ``items``/``pwyc_amount`` arrive.
    - **Cart form**: pass ``groups`` — one :class:`CartGroup` per tier in the cart.

    Exactly one of ``tier``/``groups`` must be given.
    """

    def __init__(
        self,
        event: Event,
        tier: TicketTier | None = None,
        user: RevelUser | None = None,
        discount_code: DiscountCode | None = None,
        *,
        guest_session: str | None = None,
        accessible_required: bool = False,
        price_category_id: UUID | None = None,
        groups: list[CartGroup] | None = None,
        discount_valid_tier_ids: set[UUID] | None = None,
    ) -> None:
        """Initialize the batch ticket service.

        Args:
            event: The event for which tickets are being purchased.
            tier: The ticket tier being purchased (single-tier form).
            user: The user purchasing the tickets. Required.
            discount_code: Optional validated discount code to apply.
            guest_session: Guest-hold session id for guest checkout — the browser
                held seats under this identity, not under the guest RevelUser.
            accessible_required: BEST_AVAILABLE assignment must use the accessible
                seat pool (relaxed contiguity) for the whole batch (#726).
            price_category_id: Zone the BEST_AVAILABLE pool is drawn from — a
                request parameter, validated once by
                :func:`events.service.seating.pick.resolve_requested_zone` (#749).
            groups: One :class:`CartGroup` per tier in the cart (cart form).
            discount_valid_tier_ids: The tiers ``discount_code`` was validated for.
                ``None`` means "every group" — the single-tier form's behavior, where
                the caller validated the code against the one tier it is buying. The
                cart-form controllers validate per group and pass the subset that
                passed, so a code scoped to one tier cannot leak onto the rest of the
                cart (see :meth:`_dc_for`).

        Raises:
            TypeError: If ``user`` is missing, or if ``tier``/``groups`` aren't
                given exactly one.
        """
        if user is None:
            raise TypeError("user is required")
        # `not groups` (not just `groups is None`) so an explicit groups=[] counts as
        # "not given" too — otherwise it would sail past this gate and blow up as an
        # IndexError below instead of the documented TypeError.
        if (tier is None) == (not groups):
            raise TypeError("exactly one of tier or groups is required")
        self.event = event
        self.user = user
        self.discount_code = discount_code
        self.discount_valid_tier_ids = discount_valid_tier_ids
        self.guest_session = guest_session
        self.groups: list[CartGroup] = groups or []
        # Which form the caller used — create_batch reads this instead of testing
        # self.tier, so self.tier can stay a plain TicketTier (not Optional).
        self._single_tier_form = tier is not None
        # The single-tier form's tier, kept only so ``create_batch`` can assemble that
        # form's one CartGroup from it. Nothing else may read it: every step runs per
        # group and a cart spans several tiers, so on the cart form this attribute is
        # merely the FIRST group's tier and answering any question with it would be
        # silently wrong. Do not add readers — take the tier from the group.
        self.tier: TicketTier = tier if tier is not None else self.groups[0].tier
        self.accessible_required = accessible_required
        self.price_category_id = price_category_id
        self._reserve_buyer_vat: "BuyerVATContext | None" = None

    def _dc_for(self, tier: TicketTier) -> DiscountCode | None:
        """The cart's discount code, if it applies to this tier.

        A cart's code is validated per group by the caller (organization scope, tier
        restrictions, usage limits), and a code restricted to one tier must not price
        the rest of the cart. Every pricing/stamping decision therefore asks this
        instead of reading ``self.discount_code`` directly.

        **PWYC groups never get the code, whatever the valid-ids set says.**
        ``validate_discount_code`` refuses a PWYC tier outright ("Discount codes cannot
        be applied to pay-what-you-can tickets"), so a code can never legitimately have
        been validated for one — but the ``None``-means-every-group default would hand
        it over anyway. Two things went wrong when it did: the PWYC tickets got
        ``discount_code`` stamped with a ``0.00`` ``discount_amount`` (the buyer chose
        the price; the code took nothing off), and the PWYC group's gross counted toward
        ``min_purchase_amount``, letting a threshold be met by revenue the code cannot
        touch. Enforced here rather than at the call sites so no future caller can
        reintroduce it.

        Args:
            tier: The group's tier.

        Returns:
            The code when it was validated for this tier, otherwise ``None``.
            ``discount_valid_tier_ids is None`` means "validated for every group" —
            what the single-tier form has always done.
        """
        if self.discount_code is None or tier.price_type == TicketTier.PriceType.PWYC:
            return None
        if self.discount_valid_tier_ids is None:
            return self.discount_code
        return self.discount_code if tier.pk in self.discount_valid_tier_ids else None
