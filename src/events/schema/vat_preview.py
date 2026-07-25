"""VAT preview schemas for attendee invoicing at checkout."""

from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import Field, model_validator

from .checkout import BuyerBillingInfoSchema


class VATPreviewItemSchema(Schema):
    """Single item in a VAT preview request."""

    tier_id: UUID
    count: int = Field(..., ge=1)
    price_category_id: UUID | None = Field(
        default=None,
        description=(
            "Zone the best-available picker draws from: a price category painted in the tier's "
            "sector and priced by its `category_prices` map. Null = the tier's whole pool."
        ),
    )
    seat_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "Seats chosen for this line, in cart order. Required to preview a tier that prices "
            "seats per category — without it the preview charges the tier's flat price for every "
            "ticket and will disagree with checkout. Omit for general admission. When present it "
            "must hold exactly `count` ids."
        ),
    )

    @model_validator(mode="after")
    def validate_seat_ids_cover_the_line(self) -> "VATPreviewItemSchema":
        """Partial seat context is refused: it would silently under-price the uncovered tickets."""
        if self.seat_ids and len(self.seat_ids) != self.count:
            raise ValueError("seat_ids must contain exactly `count` entries when provided.")
        return self


class VATPreviewRequestSchema(Schema):
    """Request payload for the VAT preview endpoint."""

    billing_info: BuyerBillingInfoSchema
    items: list[VATPreviewItemSchema] = Field(..., min_length=1)
    discount_code: str | None = Field(None, max_length=64, description="Optional discount code")
    price_per_ticket: Decimal | None = Field(None, ge=1, description="PWYC price override")


class VATPreviewLineItemSchema(Schema):
    """Line item in a VAT preview response.

    **One line per distinct unit price**, not per requested tier: a cart mixing price
    categories has no single ``unit_price_gross``, and collapsing it to one would be the
    exact number the buyer is not charged. A tier whose seats all cost the same — every
    tier without a category map — still yields exactly one line, unchanged.
    """

    tier_name: str
    price_category_name: str | None = Field(
        default=None,
        description=(
            "The price category this line's seats are painted with. Null for general admission, "
            "unpainted seats, and any tier without a category map."
        ),
    )
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
