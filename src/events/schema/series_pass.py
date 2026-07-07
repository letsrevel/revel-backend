"""Series pass schemas: public listing, quote, checkout, and held-pass display."""

from decimal import Decimal
from uuid import UUID

from django.utils import timezone
from ninja import ModelSchema, Schema

from events.models import HeldSeriesPass, SeriesPass
from events.models.ticket import TicketTier


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

    status: HeldSeriesPass.Status
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


class SeriesPassCheckoutResponseSchema(Schema):
    """Checkout result: a Stripe checkout URL (ONLINE) xor the created held pass (free/offline)."""

    checkout_url: str | None = None
    held_pass: HeldSeriesPassSchema | None = None
