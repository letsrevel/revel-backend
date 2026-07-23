"""Guest (unauthenticated) checkout schemas and email-confirmation JWT payloads."""

import typing as t
from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import UUID4, EmailStr, Field

from accounts.schema import BaseEmailJWTPayloadSchema
from common.schema import StrippedString

from .checkout import BuyerBillingInfoSchema, TicketPurchaseItem
from .ticket_detail import UserTicketSchema

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
    reservation_id: UUID | None = Field(
        default=None, description="Reservation handle; POST it to the checkout-session endpoint to get the Stripe URL"
    )
    requires_payment: bool = Field(
        default=False,
        description="True for online tiers: call the checkout-session endpoint next. False = already complete.",
    )


class GuestActionConfirmSchema(Schema):
    """Request to confirm a guest action via JWT token."""

    token: str = Field(..., description="JWT token from confirmation email")


# ---- Guest JWT Payload Schemas (for email confirmation tokens) ----


class GuestRSVPJWTPayloadSchema(BaseEmailJWTPayloadSchema):
    """JWT payload for guest RSVP confirmation."""

    type: t.Literal["guest_rsvp"] = "guest_rsvp"
    event_id: UUID4
    answer: t.Literal["yes", "no", "maybe"]
    note: str = Field(default="", max_length=500)


class GuestTicketItemPayload(Schema):
    """Ticket item info stored in JWT payload for guest checkout confirmation."""

    guest_name: str
    seat_id: UUID4 | None = None


class GuestTicketJWTPayloadSchema(BaseEmailJWTPayloadSchema):
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
    # Optional with default so legacy tokens minted before #726 keep validating.
    accessible_required: bool = False
    # Same reason: the best-available zone claim is absent from pre-v3 tokens.
    price_category_id: UUID4 | None = None
    # Hold-owner session captured at checkout; legacy/no-hold tokens carry None
    # and the confirm-time request cookie is used as a fallback.
    guest_session: str | None = None


# Discriminated union for guest action payloads
from pydantic import Discriminator, Tag  # noqa: E402

GuestActionPayload = t.Annotated[
    t.Union[
        t.Annotated[GuestRSVPJWTPayloadSchema, Tag("guest_rsvp")],
        t.Annotated[GuestTicketJWTPayloadSchema, Tag("guest_ticket")],
    ],
    Discriminator("type"),
]
