"""Series pass schemas: public listing, quote, checkout, held-pass display, and admin management."""

import typing as t
from decimal import Decimal
from uuid import UUID

from django.utils import timezone
from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, Field

from accounts.schema import MemberUserSchema
from common.schema import OneToOneFiftyString, StrippedString
from events.models import HeldSeriesPass, SeriesPass
from events.models.ticket import TicketTier
from events.schema.ticket import Currencies

if t.TYPE_CHECKING:
    from events.service.series_pass_service import TierLinkInput


class SeriesPassSchema(ModelSchema):
    """Public representation of a series pass."""

    payment_method: TicketTier.PaymentMethod
    purchasable_by: TicketTier.PurchasableBy

    class Meta:
        model = SeriesPass
        fields = [
            "id",
            "name",
            "description",
            "price",
            "pro_rata_discount",
            "currency",
            "payment_method",
            "purchasable_by",
            "sales_start_at",
            "sales_end_at",
        ]


class SeriesPassQuoteSchema(Schema):
    """Current pro-rata price and purchasability for a series pass."""

    price: Decimal
    passed_events: int
    remaining_events: int
    currency: str
    purchasable: bool
    reason: str | None = None


class SeriesPassSeriesInfoSchema(Schema):
    """Minimal series context nested in a held pass."""

    id: UUID
    name: str
    slug: str


class HeldSeriesPassSchema(ModelSchema):
    """A user's held series pass, with pass/series context and coverage progress.

    Query expectations: callers must ``select_related("series_pass__event_series")``
    and prefetch ``series_pass__tier_links__event`` (e.g. via a ``Prefetch`` selecting
    the tier link's event) — the count resolvers below iterate that prefetched
    relation in Python rather than issuing per-instance queries, so a list of held
    passes serializes without N+1s.
    """

    status: HeldSeriesPass.HeldSeriesPassStatus
    series_pass: SeriesPassSchema
    series: SeriesPassSeriesInfoSchema
    remaining_event_count: int
    total_event_count: int

    class Meta:
        model = HeldSeriesPass
        fields = ["id", "price_paid", "created_at"]

    @staticmethod
    def resolve_series(obj: HeldSeriesPass) -> SeriesPassSeriesInfoSchema:
        """Resolve the minimal series info. Requires ``series_pass__event_series`` select_related."""
        series = obj.series_pass.event_series
        return SeriesPassSeriesInfoSchema(id=series.id, name=series.name, slug=series.slug)

    @staticmethod
    def resolve_total_event_count(obj: HeldSeriesPass) -> int:
        """Count all events covered by the pass. Requires ``series_pass__tier_links`` prefetched."""
        return len(obj.series_pass.tier_links.all())

    @staticmethod
    def resolve_remaining_event_count(obj: HeldSeriesPass) -> int:
        """Count not-yet-started covered events. Requires ``series_pass__tier_links__event`` prefetched."""
        now = timezone.now()
        return sum(1 for link in obj.series_pass.tier_links.all() if link.event.start >= now)


class SeriesPassTierLinkAdminSchema(Schema):
    """Coverage row: the covered event and the tier backing it."""

    event_id: UUID
    event_name: str
    event_start: AwareDatetime
    tier_id: UUID
    tier_name: str


class SeriesPassAdminSchema(ModelSchema):
    """Organizer-facing series pass: public fields plus management state and coverage.

    Query expectations: annotate ``holder_count`` (active+pending holders) and prefetch
    ``tier_links`` with ``event``/``tier`` selected (see the admin controller's
    ``get_passes_queryset``) — the coverage resolver iterates the prefetched links in
    Python, so a list serializes without N+1s.
    """

    payment_method: TicketTier.PaymentMethod
    purchasable_by: TicketTier.PurchasableBy
    visibility: SeriesPass.Visibility
    holder_count: int
    tier_links: list[SeriesPassTierLinkAdminSchema]

    class Meta:
        model = SeriesPass
        fields = [
            "id",
            "name",
            "description",
            "price",
            "pro_rata_discount",
            "currency",
            "payment_method",
            "purchasable_by",
            "sales_start_at",
            "sales_end_at",
            "visibility",
            "is_active",
            "total_quantity",
        ]

    @staticmethod
    def resolve_tier_links(obj: SeriesPass) -> list[SeriesPassTierLinkAdminSchema]:
        """Serialize prefetched coverage. Requires ``tier_links`` prefetched with event/tier."""
        return [
            SeriesPassTierLinkAdminSchema(
                event_id=link.event_id,
                event_name=link.event.name,
                event_start=link.event.start,
                tier_id=link.tier_id,
                tier_name=link.tier.name,
            )
            for link in obj.tier_links.all()
        ]


class SeriesPassCheckoutResponseSchema(Schema):
    """Checkout result: a Stripe checkout URL (ONLINE) xor the created held pass (free/offline)."""

    checkout_url: str | None = None
    held_pass: HeldSeriesPassSchema | None = None
    reservation_id: UUID | None = Field(
        default=None, description="Reservation handle; POST it to the checkout-session endpoint to get the Stripe URL"
    )
    requires_payment: bool = Field(
        default=False,
        description="True for ONLINE passes: call the checkout-session endpoint next. False = already complete.",
    )


class SeriesPassTierLinkInputSchema(Schema):
    """One (event, tier) pair to cover with a series pass."""

    event_id: UUID
    tier_id: UUID


class SeriesPassCreateSchema(Schema):
    """Admin payload to create a series pass and its initial coverage."""

    name: OneToOneFiftyString
    description: StrippedString | None = None
    price: Decimal = Field(..., ge=0)
    pro_rata_discount: Decimal = Field(..., ge=0)
    currency: Currencies = Field(default="EUR", max_length=3)
    payment_method: TicketTier.PaymentMethod = TicketTier.PaymentMethod.ONLINE
    purchasable_by: TicketTier.PurchasableBy = TicketTier.PurchasableBy.PUBLIC
    visibility: SeriesPass.Visibility = SeriesPass.Visibility.PUBLIC
    sales_start_at: AwareDatetime | None = None
    sales_end_at: AwareDatetime | None = None
    total_quantity: int | None = None
    tier_links: list[SeriesPassTierLinkInputSchema] = Field(default_factory=list)

    @property
    def tier_links_as_inputs(self) -> "list[TierLinkInput]":
        """Convert ``tier_links`` to the plain (event_id, tier_id) pairs the service expects."""
        return [{"event_id": link.event_id, "tier_id": link.tier_id} for link in self.tier_links]


class SeriesPassUpdateSchema(Schema):
    """All-optional twin of ``SeriesPassCreateSchema`` for partial updates (``exclude_unset``).

    Coverage (``tier_links``) isn't editable here — use the dedicated tier-links endpoints.
    """

    name: OneToOneFiftyString | None = None
    description: StrippedString | None = None
    price: Decimal | None = Field(None, ge=0)
    pro_rata_discount: Decimal | None = Field(None, ge=0)
    currency: Currencies | None = None
    payment_method: TicketTier.PaymentMethod | None = None
    purchasable_by: TicketTier.PurchasableBy | None = None
    visibility: SeriesPass.Visibility | None = None
    sales_start_at: AwareDatetime | None = None
    sales_end_at: AwareDatetime | None = None
    total_quantity: int | None = None


class HeldSeriesPassAdminSchema(ModelSchema):
    """Admin-facing view of a held series pass: holder identity, status, and price paid."""

    status: HeldSeriesPass.HeldSeriesPassStatus
    user: MemberUserSchema

    class Meta:
        model = HeldSeriesPass
        fields = ["id", "price_paid", "created_at"]


class HeldSeriesPassCancelSchema(Schema):
    """Optional free-text reason for an admin-initiated held series pass cancellation."""

    reason: str | None = None
