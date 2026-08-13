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

        Raises:
            TypeError: If ``user`` is missing, or if ``tier``/``groups`` aren't
                given exactly one.
        """
        if user is None:
            raise TypeError("user is required")
        if (tier is None) == (groups is None):
            raise TypeError("exactly one of tier or groups is required")
        self.event = event
        self.user = user
        self.discount_code = discount_code
        self.guest_session = guest_session
        self.groups: list[CartGroup] = groups or []
        # Which form the caller used — create_batch reads this instead of testing
        # self.tier, so self.tier can stay a plain TicketTier (not Optional) for
        # every existing mixin reader below.
        self._single_tier_form = tier is not None
        # Compat attr every pre-Task-7 mixin reads directly (self.tier.price, etc.).
        # Single-tier form: the tier the caller passed. Cart form: the first (and,
        # until the cart engine lands, only) group's tier — create_batch rejects
        # more than one group before any mixin runs, so this is never wrong for a
        # tier a mixin actually sees. Removed in the final engine task — do not add
        # new readers.
        self.tier: TicketTier = tier if tier is not None else self.groups[0].tier
        self.accessible_required = accessible_required
        self.price_category_id = price_category_id
        self._reserve_buyer_vat: "BuyerVATContext | None" = None
