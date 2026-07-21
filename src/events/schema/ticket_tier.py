"""Ticket tier schemas: pricing, seating configuration, and admin CRUD."""

import typing as t
from decimal import Decimal
from uuid import UUID

from django.db.models import Q
from ninja import ModelSchema, Schema
from pydantic import UUID4, AwareDatetime, Field, model_validator

from common.schema import OneToOneFiftyString, StrippedString
from events import models
from events.models import TicketTier
from events.utils.refund_policy import RefundPolicy, RefundPolicyTier
from events.utils.tier_pricing import painted_categories, parse_price_map

from .organization import MembershipTierSchema
from .venue import TierPricingGapSchema, VenueSchema, VenueSectorSchema

# RefundPolicy + RefundPolicyTier (with the monotonic-tiers validator) live in
# events.utils.refund_policy so services, models, and schemas share one source
# of truth. Re-export under the "Schema" suffix for API documentation clarity.
RefundPolicyTierSchema = RefundPolicyTier
RefundPolicySchema = RefundPolicy

# Supported currencies — must match frankfurter.dev for exchange rate availability
Currencies = t.Literal[
    "EUR",  # Euro
    "USD",  # US Dollar
    "GBP",  # British Pound Sterling
    "JPY",  # Japanese Yen
    "AUD",  # Australian Dollar
    "CAD",  # Canadian Dollar
    "CHF",  # Swiss Franc
    "CNY",  # Chinese Yuan Renminbi
    "HKD",  # Hong Kong Dollar
    "NZD",  # New Zealand Dollar
    "SEK",  # Swedish Krona
    "KRW",  # South Korean Won
    "SGD",  # Singapore Dollar
    "NOK",  # Norwegian Krone
    "MXN",  # Mexican Peso
    "INR",  # Indian Rupee
    "ZAR",  # South African Rand
    "TRY",  # Turkish Lira
    "BRL",  # Brazilian Real
    "DKK",  # Danish Krone
    "PLN",  # Polish Zloty
    "THB",  # Thai Baht
    "IDR",  # Indonesian Rupiah
    "HUF",  # Hungarian Forint
    "CZK",  # Czech Koruna
    "ILS",  # Israeli Shekel
    "MYR",  # Malaysian Ringgit
    "PHP",  # Philippine Peso
    "RON",  # Romanian Leu
    "ISK",  # Icelandic Krona
]


class TierCategoryPriceSchema(Schema):
    """The effective price of one price category on one tier.

    Attributes:
        price: What a seat in this category costs, or ``None`` when the tier does not
            price the category — there is no honest number to show, and checkout will
            refuse the seat.
        available: False when the category is painted in the tier's sector but absent
            from the tier's price map. Seats in it must be rendered unselectable: the
            organizer has a configuration gap to fix, and buying is impossible until
            they do.
    """

    id: UUID
    name: str
    color: str
    price: Decimal | None = None
    available: bool = True


class TierSeatPricingSchema(Schema):
    """Server-resolved seat prices for a category-priced tier (spec §7).

    Deliberately *not* the raw ``category_prices`` map: handing the frontend raw rows
    would force it to reimplement the fallback chain, and any drift means the price a
    buyer is shown is not the price they are charged.

    Attributes:
        categories: What this list covers depends on the seating mode.

            - ``user_choice``: every category painted on an active seat of the tier's
              sector, plus any extra category the tier prices. A category painted *after*
              the tier was saved carries ``available=False`` and no price — checkout
              refuses those seats (spec §4.3), so quoting them a price would sell the
              buyer a 400. They are listed rather than omitted so the frontend can grey
              the seats out; silently dropping them would leave those seats unexplained
              and, worse, indistinguishable from unpainted ones.
            - ``best_available``: exactly the categories the tier prices — its sellable
              zones — all of them ``available=True``. The buyer picks a zone, not a seat,
              and a painted category the map omits is not part of this tier, so listing
              it would offer a zone that cannot be bought.
        unpainted: What a seat with no category costs, or ``None`` when no such seat can
            be bought through this tier. It is ``None`` for every ``best_available`` tier
            reaching this schema: the map is non-empty (a flat tier resolves to ``None``
            wholesale), so a zone is mandatory and the candidate pool is filtered to that
            zone's category — an unpainted seat is never a candidate. Rendering
            "Other seats: €45" from a number that can never be charged would quote an
            unbuyable price.
    """

    categories: list[TierCategoryPriceSchema] = Field(default_factory=list)
    unpainted: Decimal | None = None


class TicketTierSchema(ModelSchema):
    id: UUID
    event_id: UUID
    price: Decimal
    currency: str
    total_available: int | None
    restricted_to_membership_tiers: list[MembershipTierSchema] | None = None
    seat_assignment_mode: TicketTier.SeatAssignmentMode
    max_tickets_per_user: int | None = None
    venue: VenueSchema | None = None
    sector: VenueSectorSchema | None = None
    can_purchase: bool = True
    invoicing_available: bool = False
    refund_policy: RefundPolicySchema | None = None
    seat_pricing: TierSeatPricingSchema | None = None

    class Meta:
        model = TicketTier
        fields = [
            "id",
            "name",
            "description",
            "price",
            "price_type",
            "pwyc_min",
            "pwyc_max",
            "currency",
            "sales_start_at",
            "sales_end_at",
            "purchasable_by",
            "payment_method",
            "manual_payment_instructions",
            "seat_assignment_mode",
            "max_tickets_per_user",
            "display_order",
            "allow_user_cancellation",
            "cancellation_deadline_hours",
        ]

    @staticmethod
    def resolve_can_purchase(obj: TicketTier) -> bool:
        """Resolve from annotated attribute, defaults to True if not set."""
        return getattr(obj, "_can_purchase", True)

    @staticmethod
    def resolve_invoicing_available(obj: TicketTier) -> bool:
        """True when the org has attendee invoicing enabled and this tier uses online payment."""
        org = obj.event.organization if obj.event else None
        if not org:
            return False
        return obj.payment_method == TicketTier.PaymentMethod.ONLINE and org.invoicing_mode in (
            models.Organization.InvoicingMode.HYBRID,
            models.Organization.InvoicingMode.AUTO,
        )

    @staticmethod
    def resolve_seat_pricing(obj: TicketTier) -> TierSeatPricingSchema | None:
        """Resolve the per-category prices a buyer will actually be charged.

        ``None`` for a flat tier — that is the signal the frontend uses to decide whether
        to render a price legend at all, and it keeps the hot tier-list path at zero extra
        queries for the (overwhelmingly common) flat case. A category-priced tier costs
        exactly one query, and an event has a handful of tiers at most.

        Both seated modes are served. For ``user_choice`` the legend covers every
        category *painted* in the sector as well as every priced one, so a painted-but-
        unpriced category shows up as unavailable — the buyer can click that seat and
        must be told it cannot be sold. For ``best_available`` the buyer never picks a
        seat, only a zone, and the map keys *are* the zones: unpriced painted categories
        are not part of this tier and are omitted rather than shown as unavailable — and
        for the same reason ``unpainted`` is ``None``, since the zone-filtered pool can
        never yield an unpainted seat.
        """
        price_map = parse_price_map(obj.category_prices)
        # A priced map without a venue cannot exist — tier validation rejects categories
        # that don't belong to the tier's venue — so the venue guard is only for mypy.
        if not price_map or obj.venue_id is None:
            return None
        in_scope = Q(id__in=list(price_map))
        if obj.seat_assignment_mode == TicketTier.SeatAssignmentMode.USER_CHOICE:
            in_scope |= Q(seats__sector_id=obj.sector_id, seats__is_active=True)
        categories = (
            models.PriceCategory.objects.filter(in_scope, venue_id=obj.venue_id)
            .distinct()
            .order_by("display_order", "name")
        )
        return TierSeatPricingSchema(
            categories=[
                TierCategoryPriceSchema(
                    id=category.id,
                    name=category.name,
                    color=category.color,
                    # Everything in this queryset is either priced or painted, so "absent from
                    # the map" is exactly "painted but unpriced" — the case checkout refuses.
                    price=price_map.get(category.id),
                    available=category.id in price_map,
                )
                for category in categories
            ],
            unpainted=None if obj.seat_assignment_mode == TicketTier.SeatAssignmentMode.BEST_AVAILABLE else obj.price,
        )


# ---- TicketTier Schemas for Admin CRUD ----

# The per-seat-category price map (``{price_category_id: decimal-string}``).
#
# Deliberately typed as an opaque JSON object rather than ``dict[UUID4, Decimal]``:
# pydantic would coerce a JSON float such as ``50.0`` into a Decimal and silently
# persist binary-float money. The map is passed through untouched and validated in
# exactly one place — ``events.utils.tier_pricing.parse_price_map``, reached from
# ``TicketTier.clean()`` — which rejects floats and bools outright. Malformed input
# therefore surfaces as a Django ``ValidationError`` mapped to HTTP 400, never a 500
# and never a silent coercion.
CategoryPriceMap = dict[str, t.Any]


class TicketTierPriceValidationMixin(Schema):
    payment_method: TicketTier.PaymentMethod = TicketTier.PaymentMethod.OFFLINE
    price: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def validate_minimum_price(self) -> t.Self:
        """Validate the minimum price for ONLINE payments."""
        if self.payment_method == TicketTier.PaymentMethod.ONLINE and self.price < Decimal("1"):
            raise ValueError("Minimum price for ONLINE payments should be at least 1.")
        return self


class TicketTierCreateSchema(TicketTierPriceValidationMixin):
    name: OneToOneFiftyString
    description: StrippedString | None = None
    visibility: TicketTier.Visibility = TicketTier.Visibility.PUBLIC
    purchasable_by: TicketTier.PurchasableBy = TicketTier.PurchasableBy.PUBLIC
    restrict_visibility_to_linked_invitations: bool = False
    restrict_purchase_to_linked_invitations: bool = False
    price_type: TicketTier.PriceType = TicketTier.PriceType.FIXED
    pwyc_min: Decimal = Field(default=Decimal("1"), ge=1)
    pwyc_max: Decimal | None = Field(None, ge=1)
    vat_rate: Decimal | None = Field(None, ge=0, le=100, description="VAT rate override. Null = use org default.")

    currency: Currencies = Field(default="EUR", max_length=3)
    sales_start_at: AwareDatetime | None = None
    sales_end_at: AwareDatetime | None = None
    total_quantity: int | None = None
    restricted_to_membership_tiers_ids: list[UUID4] | None = None
    manual_payment_instructions: StrippedString | None = None

    # Venue/seating configuration
    seat_assignment_mode: TicketTier.SeatAssignmentMode = TicketTier.SeatAssignmentMode.NONE
    max_tickets_per_user: int | None = None
    venue_id: UUID | None = None
    sector_id: UUID | None = None
    category_prices: CategoryPriceMap | None = Field(
        default=None,
        description=(
            "Per-seat-category prices for seated tiers: {price_category_id: decimal-string}. "
            "For user-choice tiers every painted category must be priced; for best-available the "
            "keys define the tier's sellable zones (partial coverage is allowed). "
            "Omitted or null leaves the map at its default (empty); an empty object clears it; a "
            "non-empty object replaces it wholesale. Prices must be decimal strings or integers — "
            "JSON floats are rejected, because binary floats cannot represent money."
        ),
    )

    # None (or omitted) means "append at the bottom"; an explicit value pins the position (#514).
    display_order: int | None = None

    allow_user_cancellation: bool = False
    cancellation_deadline_hours: int | None = Field(default=None, ge=0)
    refund_policy: RefundPolicySchema | None = None

    @model_validator(mode="after")
    def validate_pwyc_fields(self) -> t.Self:
        """Validate PWYC fields consistency."""
        if self.price_type == TicketTier.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_max < self.pwyc_min:
                raise ValueError("PWYC maximum must be greater than or equal to minimum.")
        return self

    @model_validator(mode="after")
    def validate_seat_assignment_requires_sector(self) -> t.Self:
        """Validate that each seat assignment mode comes with the field it reads."""
        if self.seat_assignment_mode == TicketTier.SeatAssignmentMode.BEST_AVAILABLE and self.sector_id is None:
            raise ValueError("A sector is required when seat assignment mode is BEST_AVAILABLE.")
        if self.seat_assignment_mode == TicketTier.SeatAssignmentMode.USER_CHOICE and self.sector_id is None:
            raise ValueError("A sector is required when seat assignment mode is USER_CHOICE.")
        return self


class TicketTierUpdateSchema(TicketTierPriceValidationMixin):
    name: OneToOneFiftyString | None = None
    description: StrippedString | None = None
    visibility: TicketTier.Visibility | None = None
    purchasable_by: TicketTier.PurchasableBy | None = None
    restrict_visibility_to_linked_invitations: bool | None = None
    restrict_purchase_to_linked_invitations: bool | None = None
    price_type: TicketTier.PriceType | None = None
    pwyc_min: Decimal | None = Field(None, ge=1)
    pwyc_max: Decimal | None = Field(None, ge=1)
    vat_rate: Decimal | None = Field(None, ge=0, le=100, description="VAT rate override. Null = use org default.")
    currency: Currencies | None = None
    sales_start_at: AwareDatetime | None = None
    sales_end_at: AwareDatetime | None = None
    total_quantity: int | None = None
    restricted_to_membership_tiers_ids: list[UUID4] | None = None
    manual_payment_instructions: StrippedString | None = None

    # Venue/seating configuration
    seat_assignment_mode: TicketTier.SeatAssignmentMode | None = None
    max_tickets_per_user: int | None = None
    venue_id: UUID | None = None
    sector_id: UUID | None = None
    category_prices: CategoryPriceMap | None = Field(
        default=None,
        description=(
            "Per-seat-category prices for seated tiers: {price_category_id: decimal-string}. "
            "For user-choice tiers every painted category must be priced; for best-available the "
            "keys define the tier's sellable zones (partial coverage is allowed). "
            "Omitted or null leaves the existing map untouched; an empty object clears it; a "
            "non-empty object replaces it wholesale. Prices must be decimal strings or integers — "
            "JSON floats are rejected, because binary floats cannot represent money."
        ),
    )

    display_order: int | None = None

    allow_user_cancellation: bool | None = None
    cancellation_deadline_hours: int | None = Field(default=None, ge=0)
    refund_policy: RefundPolicySchema | None = None

    @model_validator(mode="after")
    def validate_pwyc_fields(self) -> t.Self:
        """Validate PWYC fields consistency."""
        if self.price_type == TicketTier.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_min and self.pwyc_max < self.pwyc_min:
                raise ValueError("PWYC maximum must be greater than or equal to minimum.")
        return self

    @model_validator(mode="after")
    def validate_seat_assignment_requires_sector(self) -> t.Self:
        """Validate that a mode being explicitly set comes with the field it reads."""
        if self.seat_assignment_mode == TicketTier.SeatAssignmentMode.BEST_AVAILABLE and self.sector_id is None:
            raise ValueError("A sector is required when seat assignment mode is BEST_AVAILABLE.")
        if self.seat_assignment_mode == TicketTier.SeatAssignmentMode.USER_CHOICE and self.sector_id is None:
            raise ValueError("A sector is required when seat assignment mode is USER_CHOICE.")
        return self


class TicketTierDetailSchema(ModelSchema):
    event_id: UUID
    total_available: int | None = None
    restricted_to_membership_tiers: list[MembershipTierSchema] | None = None
    seat_assignment_mode: TicketTier.SeatAssignmentMode
    max_tickets_per_user: int | None = None
    venue: VenueSchema | None = None
    sector: VenueSectorSchema | None = None
    vat_rate: Decimal | None = None
    invoicing_available: bool = False
    refund_policy: RefundPolicySchema | None = None
    category_prices: CategoryPriceMap = Field(default_factory=dict)
    pricing_gaps: list[TierPricingGapSchema] = Field(default_factory=list)

    class Meta:
        model = TicketTier
        fields = [
            "id",
            "name",
            "description",
            "visibility",
            "payment_method",
            "purchasable_by",
            "restrict_visibility_to_linked_invitations",
            "restrict_purchase_to_linked_invitations",
            "price",
            "price_type",
            "pwyc_min",
            "pwyc_max",
            "currency",
            "sales_start_at",
            "sales_end_at",
            "created_at",
            "updated_at",
            "total_quantity",
            "quantity_sold",
            "manual_payment_instructions",
            "restricted_to_membership_tiers",
            "seat_assignment_mode",
            "max_tickets_per_user",
            "display_order",
            "vat_rate",
            "allow_user_cancellation",
            "cancellation_deadline_hours",
        ]

    @staticmethod
    def resolve_invoicing_available(obj: TicketTier) -> bool:
        """True when the org has attendee invoicing enabled and this tier uses online payment."""
        org = obj.event.organization if obj.event else None
        if not org:
            return False
        return obj.payment_method == TicketTier.PaymentMethod.ONLINE and org.invoicing_mode in (
            models.Organization.InvoicingMode.HYBRID,
            models.Organization.InvoicingMode.AUTO,
        )

    @staticmethod
    def resolve_pricing_gaps(obj: TicketTier) -> list[TierPricingGapSchema]:
        """Categories painted in the tier's sector that the map does not price.

        A configuration error only the organizer can fix: ``paint_seats`` is venue-scoped
        and deliberately never fails (spec §4.3), so a repaint at the venue level can
        leave an already-saved tier out of step with its sector. Nothing else tells the
        admin, so the tier form warns from this field. Two distinct cases produce a gap:

        - **A mapped user-choice tier missing a painted category.** Write-time validation
          demanded full coverage; a later repaint broke it, and checkout now refuses those
          seats.
        - **A tier of either mode with an *empty* map over a painted sector.** Flat pricing
          is a legitimate choice — an organizer may paint purely for colour-coding — so this
          is never rejected at write time. But it also silently sells a premium seat at the
          flat price, which is the exact mispricing this feature exists to prevent, so the
          organizer is shown what they are flattening. Advisory, not an error.

        A **mapped best-available** tier reports nothing: there the map keys define the
        tier's sellable zones, so a painted category the map omits is not a gap — it is
        simply not a zone of this tier, and reporting it would be a permanent false alarm.
        A tier whose sector carries no paint at all likewise reports nothing, in every mode.

        Computed, never stored — a stored flag would desync on the next repaint.

        # ponytail: one query per *seated* tier on the admin tier list (unseated tiers cost
        # nothing). An event has a handful of tiers, so this is well under the noise floor;
        # if that ever changes, prefetch the sectors' painted categories once in
        # ``list_ticket_tiers`` and resolve from that map.
        """
        if obj.seat_assignment_mode == TicketTier.SeatAssignmentMode.NONE:
            return []
        price_map = parse_price_map(obj.category_prices)
        if price_map and obj.seat_assignment_mode != TicketTier.SeatAssignmentMode.USER_CHOICE:
            return []
        gaps = painted_categories(obj.sector_id).exclude(id__in=list(price_map)).order_by("display_order", "name")
        return [TierPricingGapSchema(id=c.id, name=c.name, color=c.color) for c in gaps]


class ReorderSchema(Schema):
    tier_ids: list[UUID]
