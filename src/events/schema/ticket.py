"""Ticket, payment, and checkout schemas."""

import typing as t
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import UUID4, AwareDatetime, EmailStr, Field, field_validator, model_validator

from accounts.schema import MemberUserSchema, MinimalRevelUserSchema, _BaseEmailJWTPayloadSchema
from common.schema import OneToOneFiftyString, StrippedString, validate_country_code
from common.signing import get_file_url
from events import models
from events.models import Payment, Ticket, TicketTier

from .event import MinimalEventSchema
from .organization import MembershipTierSchema, MinimalOrganizationMemberSchema
from .venue import MinimalSeatSchema, VenueSchema, VenueSectorSchema

# Supported currencies — must match frankfurter.app for exchange rate availability
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
        ]

    @staticmethod
    def resolve_can_purchase(obj: TicketTier) -> bool:
        """Resolve from annotated attribute, defaults to True if not set."""
        return getattr(obj, "_can_purchase", True)


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
    price_paid: Decimal | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "tier", "created_at", "guest_name", "seat", "price_paid"]

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
    price_paid: Decimal | None = None
    discount_amount: Decimal | None = None
    pdf_url: str | None = None
    pkpass_url: str | None = None

    class Meta:
        model = Ticket
        fields = [
            "id",
            "status",
            "tier",
            "created_at",
            "checked_in_at",
            "guest_name",
            "seat",
            "price_paid",
            "discount_amount",
        ]

    @staticmethod
    def resolve_payment(obj: Ticket) -> Payment | None:
        """Resolve payment for pending tickets."""
        if hasattr(obj, "payment"):
            return obj.payment
        return None

    @staticmethod
    def resolve_pdf_url(obj: Ticket) -> str | None:
        """Resolve cached PDF file to signed URL."""
        return get_file_url(obj.pdf_file)

    @staticmethod
    def resolve_pkpass_url(obj: Ticket) -> str | None:
        """Resolve cached pkpass file to signed URL."""
        return get_file_url(obj.pkpass_file)


class CheckInRequestSchema(Schema):
    """Schema for ticket check-in requests."""

    ticket_id: UUID


class CheckInResponseSchema(ModelSchema):
    """Schema for ticket check-in response."""

    user: MinimalRevelUserSchema
    tier: TicketTierSchema | None = None
    price_paid: Decimal | None = None

    class Meta:
        model = Ticket
        fields = ["id", "status", "checked_in_at", "tier", "price_paid"]


class ConfirmPaymentSchema(Schema):
    """Optional payload for confirming offline/at-the-door ticket payment.

    price_paid is required for PWYC tiers and must be omitted for fixed-price tiers.
    """

    price_paid: Decimal | None = Field(None, gt=0)


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

    display_order: int = 0

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
    restrict_visibility_to_linked_invitations: bool | None = None
    restrict_purchase_to_linked_invitations: bool | None = None
    price_type: TicketTier.PriceType | None = None
    pwyc_min: Decimal | None = Field(None, ge=1)
    pwyc_max: Decimal | None = Field(None, ge=1)
    vat_rate: Decimal | None = Field(None, ge=0, le=100, description="VAT rate override. Null = use org default.")
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

    display_order: int | None = None

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
    vat_rate: Decimal | None = None

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
        ]


class ReorderSchema(Schema):
    tier_ids: list[UUID]


# --- Stripe Schemas ---

# StripeOnboardingLinkSchema and StripeAccountStatusSchema live in common.schema
# (shared with accounts/referral). Re-exported here for backwards compatibility.
from common.schema import StripeAccountStatusSchema as StripeAccountStatusSchema  # noqa: F401, E402
from common.schema import StripeOnboardingLinkSchema as StripeOnboardingLinkSchema  # noqa: F401, E402


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


class BuyerBillingInfoSchema(Schema):
    """Buyer billing info for attendee invoicing at checkout."""

    billing_name: str = Field(..., min_length=1, max_length=255)
    vat_id: str = Field("", max_length=20)
    vat_country_code: str = Field("", max_length=2)
    billing_address: str = ""
    billing_email: str = ""
    save_to_profile: bool = False

    @field_validator("vat_country_code")
    @classmethod
    def validate_vat_country_code(cls, v: str) -> str:
        """Validate ISO 3166-1 alpha-2 country code or allow empty."""
        return validate_country_code(v) or ""

    @field_validator("billing_email")
    @classmethod
    def validate_billing_email(cls, v: str) -> str:
        """Allow empty string but reject invalid emails."""
        if v:
            from pydantic import TypeAdapter

            TypeAdapter(EmailStr).validate_python(v)
        return v


class VATPreviewItemSchema(Schema):
    """Single item in a VAT preview request."""

    tier_id: UUID
    count: int = Field(..., ge=1)


class VATPreviewRequestSchema(Schema):
    """Request payload for the VAT preview endpoint."""

    billing_info: BuyerBillingInfoSchema
    items: list[VATPreviewItemSchema] = Field(..., min_length=1)
    discount_code: str | None = Field(None, max_length=64, description="Optional discount code")
    price_per_ticket: Decimal | None = Field(None, ge=0, description="PWYC price override")


class VATPreviewLineItemSchema(Schema):
    """Line item in a VAT preview response."""

    tier_name: str
    ticket_count: int
    unit_price_gross: Decimal
    unit_price_net: Decimal
    unit_vat: Decimal
    vat_rate: Decimal
    line_net: Decimal
    line_vat: Decimal
    line_gross: Decimal


class VATPreviewResponseSchema(Schema):
    """Response from the VAT preview endpoint."""

    vat_id_valid: bool | None = None
    vat_id_validation_error: str | None = None
    reverse_charge: bool
    line_items: list[VATPreviewLineItemSchema]
    total_net: Decimal
    total_vat: Decimal
    total_gross: Decimal
    currency: str


class BatchCheckoutPayload(Schema):
    """Payload for batch ticket checkout (authenticated users)."""

    tickets: list[TicketPurchaseItem] = Field(..., min_length=1, description="List of tickets to purchase")
    discount_code: str | None = Field(None, max_length=64, description="Optional discount code")
    billing_info: BuyerBillingInfoSchema | None = Field(None, description="Optional billing info for invoicing")


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
    discount_code: str | None = Field(None, max_length=64, description="Optional discount code")


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
    discount_code: str | None = None
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
