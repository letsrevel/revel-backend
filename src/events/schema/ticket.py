"""Ticket, payment, and checkout schemas."""

import typing as t
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import UUID4, AwareDatetime, EmailStr, Field, model_validator

from accounts.schema import MemberUserSchema, MinimalRevelUserSchema, _BaseEmailJWTPayloadSchema
from common.schema import OneToOneFiftyString, StrippedString
from events import models
from events.models import Payment, Ticket, TicketTier

from .event import MinimalEventSchema
from .organization import MembershipTierSchema, MinimalOrganizationMemberSchema
from .venue import MinimalSeatSchema, VenueSchema, VenueSectorSchema

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
    "RUB",  # Russian Ruble
    "ZAR",  # South African Rand
    "TRY",  # Turkish Lira
    "BRL",  # Brazilian Real
    "TWD",  # New Taiwan Dollar
    "DKK",  # Danish Krone
    "PLN",  # Polish Zloty
    "THB",  # Thai Baht
    "IDR",  # Indonesian Rupiah
    "HUF",  # Hungarian Forint
    "CZK",  # Czech Koruna
    "ILS",  # Israeli Shekel
    "AED",  # UAE Dirham
    "SAR",  # Saudi Riyal
    "MYR",  # Malaysian Ringgit
    "PHP",  # Philippine Peso
    "CLP",  # Chilean Peso
    "COP",  # Colombian Peso
    "PKR",  # Pakistani Rupee
    "EGP",  # Egyptian Pound
    "NGN",  # Nigerian Naira
    "VND",  # Vietnamese Dong
    "BDT",  # Bangladeshi Taka
    "ARS",  # Argentine Peso
    "QAR",  # Qatari Riyal
    "KWD",  # Kuwaiti Dinar
    "BHD",  # Bahraini Dinar
    "OMR",  # Omani Rial
    "MAD",  # Moroccan Dirham
    "KES",  # Kenyan Shilling
    "UAH",  # Ukrainian Hryvnia
    "RON",  # Romanian Leu
    "BGN",  # Bulgarian Lev
    "HRK",  # Croatian Kuna (still valid for legacy data)
    "ISK",  # Icelandic Krona
]


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
        ]


class PaymentSchema(ModelSchema):
    """Public representation of a Payment record."""

    status: Payment.PaymentStatus
    currency: Currencies
    stripe_dashboard_url: str

    class Meta:
        model = Payment
        exclude = ["user", "ticket", "raw_response"]


class MinimalPaymentSchema(ModelSchema):
    """Minimal payment info for inclusion in ticket schemas."""

    status: Payment.PaymentStatus

    class Meta:
        model = Payment
        fields = ["id", "status"]


class AdminTicketSchema(ModelSchema):
    """Schema for pending tickets in admin interface.

    Venue and sector info comes from tier (tier.venue, tier.sector).
    Only seat is included at ticket level for assigned seating.
    """

    user: MemberUserSchema
    tier: TicketTierSchema
    payment: PaymentSchema | None = None
    guest_name: str
    seat: MinimalSeatSchema | None = None
    membership: MinimalOrganizationMemberSchema | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "created_at", "guest_name", "seat"]

    @staticmethod
    def resolve_membership(obj: Ticket) -> models.OrganizationMember | None:
        """Resolve membership from prefetched org_membership_list."""
        memberships = getattr(obj.user, "org_membership_list", None)
        return memberships[0] if memberships else None


class UserTicketSchema(ModelSchema):
    """Schema for user's own tickets with event details.

    Venue and sector info comes from tier (tier.venue, tier.sector).
    Only seat is included at ticket level for assigned seating.
    """

    event: MinimalEventSchema
    tier: TicketTierSchema
    status: Ticket.TicketStatus
    apple_pass_available: bool
    guest_name: str
    payment: MinimalPaymentSchema | None = None
    seat: MinimalSeatSchema | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "created_at", "checked_in_at", "guest_name", "seat"]

    @staticmethod
    def resolve_payment(obj: Ticket) -> Payment | None:
        """Resolve payment for pending tickets."""
        if hasattr(obj, "payment"):
            return obj.payment
        return None


class CheckInRequestSchema(Schema):
    """Schema for ticket check-in requests."""

    ticket_id: UUID


class CheckInResponseSchema(ModelSchema):
    """Schema for ticket check-in response."""

    user: MinimalRevelUserSchema
    tier: TicketTierSchema | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "checked_in_at", "tier"]


# ---- TicketTier Schemas for Admin CRUD ----


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
    price_type: TicketTier.PriceType = TicketTier.PriceType.FIXED
    pwyc_min: Decimal = Field(default=Decimal("1"), ge=1)
    pwyc_max: Decimal | None = Field(None, ge=1)

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

    @model_validator(mode="after")
    def validate_pwyc_fields(self) -> t.Self:
        """Validate PWYC fields consistency."""
        if self.price_type == TicketTier.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_max < self.pwyc_min:
                raise ValueError("PWYC maximum must be greater than or equal to minimum.")
        return self

    @model_validator(mode="after")
    def validate_seat_assignment_requires_sector(self) -> t.Self:
        """Validate that seat assignment modes require a sector."""
        if self.seat_assignment_mode != TicketTier.SeatAssignmentMode.NONE and self.sector_id is None:
            raise ValueError("Sector is required when seat assignment mode is not NONE.")
        return self


class TicketTierUpdateSchema(TicketTierPriceValidationMixin):
    name: OneToOneFiftyString | None = None
    description: StrippedString | None = None
    visibility: TicketTier.Visibility | None = None
    purchasable_by: TicketTier.PurchasableBy | None = None
    price_type: TicketTier.PriceType | None = None
    pwyc_min: Decimal | None = Field(None, ge=1)
    pwyc_max: Decimal | None = Field(None, ge=1)
    currency: str | None = Field(None, max_length=3)
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

    @model_validator(mode="after")
    def validate_pwyc_fields(self) -> t.Self:
        """Validate PWYC fields consistency."""
        if self.price_type == TicketTier.PriceType.PWYC:
            if self.pwyc_max and self.pwyc_min and self.pwyc_max < self.pwyc_min:
                raise ValueError("PWYC maximum must be greater than or equal to minimum.")
        return self

    @model_validator(mode="after")
    def validate_seat_assignment_requires_sector(self) -> t.Self:
        """Validate that seat assignment modes require a sector when being set."""
        # Only validate if seat_assignment_mode is being explicitly set to a non-NONE value
        if (
            self.seat_assignment_mode is not None
            and self.seat_assignment_mode != TicketTier.SeatAssignmentMode.NONE
            and self.sector_id is None
        ):
            raise ValueError("Sector is required when seat assignment mode is not NONE.")
        return self


class TicketTierDetailSchema(ModelSchema):
    event_id: UUID
    total_available: int | None = None
    restricted_to_membership_tiers: list[MembershipTierSchema] | None = None
    seat_assignment_mode: TicketTier.SeatAssignmentMode
    max_tickets_per_user: int | None = None
    venue: VenueSchema | None = None
    sector: VenueSectorSchema | None = None

    class Meta:
        model = TicketTier
        fields = [
            "id",
            "name",
            "description",
            "visibility",
            "payment_method",
            "purchasable_by",
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
        ]


# --- Stripe Schemas ---


class StripeOnboardingLinkSchema(Schema):
    onboarding_url: str


class StripeAccountStatusSchema(Schema):
    is_connected: bool
    charges_enabled: bool = False
    details_submitted: bool = False


class StripeCheckoutSessionSchema(Schema):
    checkout_url: str


class PWYCCheckoutPayloadSchema(Schema):
    """Schema for Pay What You Can checkout payload."""

    pwyc: Decimal = Field(..., ge=1, description="Pay what you can amount, minimum 1")


# ---- Batch Checkout Schemas ----


class TicketPurchaseItem(Schema):
    """Single ticket item in a batch purchase."""

    guest_name: StrippedString = Field(..., min_length=1, max_length=255, description="Name of the ticket holder")
    seat_id: UUID | None = Field(default=None, description="Seat ID for USER_CHOICE seat assignment mode")


class BatchCheckoutPayload(Schema):
    """Payload for batch ticket checkout (authenticated users)."""

    tickets: list[TicketPurchaseItem] = Field(..., min_length=1, description="List of tickets to purchase")


class BatchCheckoutPWYCPayload(BatchCheckoutPayload):
    """Payload for batch PWYC ticket checkout."""

    price_per_ticket: Decimal = Field(..., ge=1, description="Pay what you can amount per ticket (same for all)")


class BatchCheckoutResponse(Schema):
    """Response for batch checkout operations."""

    checkout_url: str | None = Field(None, description="Stripe checkout URL (for online payment)")
    tickets: list[UserTicketSchema] = Field(
        default_factory=list, description="Created tickets (for free/offline payments)"
    )


# ---- Guest User Schemas ----


class GuestUserDataSchema(Schema):
    """Base schema for guest user data (no authentication required)."""

    email: EmailStr
    first_name: StrippedString = Field(..., min_length=1, max_length=150, description="Guest user's first name")
    last_name: StrippedString = Field(..., min_length=1, max_length=150, description="Guest user's last name")


class GuestPWYCCheckoutSchema(GuestUserDataSchema):
    """Schema for guest PWYC ticket checkout."""

    pwyc: Decimal = Field(..., ge=1, description="Pay what you can amount, minimum 1")


class GuestBatchCheckoutPayload(GuestUserDataSchema):
    """Payload for batch checkout by guest (unauthenticated) users."""

    tickets: list[TicketPurchaseItem] = Field(..., min_length=1, description="List of tickets to purchase")


class GuestBatchCheckoutPWYCPayload(GuestBatchCheckoutPayload):
    """Payload for batch PWYC checkout by guest users."""

    price_per_ticket: Decimal = Field(..., ge=1, description="Pay what you can amount per ticket (same for all)")


class GuestActionResponseSchema(Schema):
    """Response after guest action initiated (RSVP or non-online-payment ticket)."""

    message: str = Field(default="Please check your email to confirm your action")


class GuestCheckoutResponseSchema(Schema):
    """Combined response for guest checkout - either email confirmation or Stripe checkout."""

    # For non-online payments (email confirmation)
    message: str | None = Field(None, description="Confirmation message (for non-online payments)")
    # For online payments (Stripe checkout)
    checkout_url: str | None = Field(None, description="Stripe checkout URL (for online payment)")
    tickets: list[UserTicketSchema] = Field(
        default_factory=list,
        description="Created tickets (only present after guest email confirmation for free/offline payments)",
    )


class GuestActionConfirmSchema(Schema):
    """Request to confirm a guest action via JWT token."""

    token: str = Field(..., description="JWT token from confirmation email")


# ---- Guest JWT Payload Schemas (for email confirmation tokens) ----


class GuestRSVPJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    """JWT payload for guest RSVP confirmation."""

    type: t.Literal["guest_rsvp"] = "guest_rsvp"
    event_id: UUID4
    answer: t.Literal["yes", "no", "maybe"]


class GuestTicketItemPayload(Schema):
    """Ticket item info stored in JWT payload for guest checkout confirmation."""

    guest_name: str
    seat_id: UUID4 | None = None


class GuestTicketJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    """JWT payload for guest ticket purchase confirmation.

    Only used for non-online-payment tickets (free/offline/at-the-door).
    Online payment tickets go directly to Stripe without email confirmation.
    """

    type: t.Literal["guest_ticket"] = "guest_ticket"
    event_id: UUID4
    tier_id: UUID4
    pwyc_amount: Decimal | None = None
    tickets: list[GuestTicketItemPayload] = Field(default_factory=list)


# Discriminated union for guest action payloads
from pydantic import Discriminator, Tag  # noqa: E402

GuestActionPayload = t.Annotated[
    t.Union[
        t.Annotated[GuestRSVPJWTPayloadSchema, Tag("guest_rsvp")],
        t.Annotated[GuestTicketJWTPayloadSchema, Tag("guest_ticket")],
    ],
    Discriminator("type"),
]
