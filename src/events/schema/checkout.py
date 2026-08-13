"""Checkout payloads and responses for ticket purchases."""

from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import EmailStr, Field, field_validator

# --- Stripe Schemas ---
# StripeOnboardingLinkSchema and StripeAccountStatusSchema live in common.schema
# (shared with accounts/referral). Re-exported here for backwards compatibility.
from common.schema import StripeAccountStatusSchema as StripeAccountStatusSchema  # noqa: F401, E402
from common.schema import StripeOnboardingLinkSchema as StripeOnboardingLinkSchema  # noqa: F401, E402
from common.schema import StrippedString, validate_country_code

from .ticket_detail import UserTicketSchema


class StripeCheckoutSessionSchema(Schema):
    checkout_url: str


class PWYCCheckoutPayloadSchema(Schema):
    """Schema for Pay What You Can checkout payload."""

    pwyc: Decimal = Field(..., ge=1, description="Pay what you can amount, minimum 1")


# ---- Batch Checkout Schemas ----


class TicketPurchaseItem(Schema):
    """Single ticket item in a batch purchase."""

    guest_name: StrippedString | None = Field(
        default=None, max_length=255, description="Name of the ticket holder (optional unless the event requires it)"
    )
    seat_id: UUID | None = Field(default=None, description="Seat ID for USER_CHOICE seat assignment mode")

    @field_validator("guest_name", mode="after")
    @classmethod
    def _empty_name_to_none(cls, v: str | None) -> str | None:
        """One 'absent' representation downstream: whitespace-only input becomes None."""
        return v or None


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


class BatchCheckoutPayload(Schema):
    """Payload for batch ticket checkout (authenticated users)."""

    tickets: list[TicketPurchaseItem] = Field(..., min_length=1, description="List of tickets to purchase")
    discount_code: str | None = Field(None, max_length=64, description="Optional discount code")
    billing_info: BuyerBillingInfoSchema | None = Field(None, description="Optional billing info for invoicing")
    accessible_required: bool = Field(
        default=False,
        description="Request accessible seating for the whole checkout (BEST_AVAILABLE assignment "
        "picks from the accessible pool)",
    )
    price_category_id: UUID | None = Field(
        default=None,
        description=(
            "Zone the best-available picker draws from: a price category painted in the tier's "
            "sector and priced by its `category_prices` map. Null = the tier's whole pool."
        ),
    )


class BatchCheckoutPWYCPayload(BatchCheckoutPayload):
    """Payload for batch PWYC ticket checkout."""

    price_per_ticket: Decimal = Field(..., ge=1, description="Pay what you can amount per ticket (same for all)")


# ---- Multi-Tier Cart Checkout Schemas (#846) ----


class CheckoutGroupSchema(Schema):
    """One tier's slice of a multi-tier checkout cart."""

    tier_id: UUID
    tickets: list[TicketPurchaseItem] = Field(..., min_length=1)
    pwyc_amount: Decimal | None = Field(default=None, ge=0, description="Required iff the tier is pay-what-you-can")
    price_category_id: UUID | None = Field(default=None, description="Zone the best-available picker draws from")
    accessible_required: bool = Field(
        default=False, description="Accessible pool for this group's BEST_AVAILABLE block"
    )


class MultiTierCheckoutPayload(Schema):
    """Cart payload for POST /events/{event_id}/checkout."""

    items: list[CheckoutGroupSchema] = Field(..., min_length=1, max_length=20)
    discount_code: str | None = Field(default=None, max_length=64)
    billing_info: BuyerBillingInfoSchema | None = Field(default=None)


class BatchCheckoutResponse(Schema):
    """Response for batch checkout operations."""

    checkout_url: str | None = Field(None, description="Stripe checkout URL (for online payment)")
    tickets: list[UserTicketSchema] = Field(
        default_factory=list, description="Created tickets (for free/offline payments)"
    )
    reservation_id: UUID | None = Field(
        default=None, description="Reservation handle; POST it to the checkout-session endpoint to get the Stripe URL"
    )
    requires_payment: bool = Field(
        default=False,
        description="True for online tiers: call the checkout-session endpoint next. False = already complete.",
    )


class CheckoutSessionResponse(Schema):
    """Response of the checkout-session endpoint: the Stripe URL to redirect to."""

    checkout_url: str = Field(..., description="Stripe checkout URL")
