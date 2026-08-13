"""Request-scoped state shared by every ``BatchTicketService`` mixin."""

import dataclasses
import typing as t
from decimal import Decimal
from uuid import UUID

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
