"""Request-scoped state shared by every ``BatchTicketService`` mixin."""

import typing as t
from uuid import UUID

from accounts.models import RevelUser
from events.models import Event, TicketTier
from events.models.discount_code import DiscountCode

if t.TYPE_CHECKING:
    from events.service.attendee_vat_service import BuyerVATContext


class BatchTicketContext:
    """The inputs of a single batch purchase.

    Every mixin in this package inherits from this class so ``self.event``,
    ``self.tier`` & co. are declared exactly once and type-check under
    ``mypy --strict``. It is never instantiated directly — see
    :class:`~events.service.batch_ticket_service.service.BatchTicketService`.
    """

    def __init__(
        self,
        event: Event,
        tier: TicketTier,
        user: RevelUser,
        discount_code: DiscountCode | None = None,
        *,
        guest_session: str | None = None,
        accessible_required: bool = False,
        price_category_id: UUID | None = None,
    ) -> None:
        """Initialize the batch ticket service.

        Args:
            event: The event for which tickets are being purchased.
            tier: The ticket tier being purchased.
            user: The user purchasing the tickets.
            discount_code: Optional validated discount code to apply.
            guest_session: Guest-hold session id for guest checkout — the browser
                held seats under this identity, not under the guest RevelUser.
            accessible_required: BEST_AVAILABLE assignment must use the accessible
                seat pool (relaxed contiguity) for the whole batch (#726).
            price_category_id: Zone the BEST_AVAILABLE pool is drawn from — a
                request parameter, validated once by
                :func:`events.service.seating.pick.resolve_requested_zone` (#749).
        """
        self.event = event
        self.tier = tier
        self.user = user
        self.discount_code = discount_code
        self.guest_session = guest_session
        self.accessible_required = accessible_required
        self.price_category_id = price_category_id
        self._reserve_buyer_vat: "BuyerVATContext | None" = None
