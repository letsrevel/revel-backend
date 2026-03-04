"""Discount code schemas for admin CRUD and public validation."""

import typing as t
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, Field, model_validator

from events.models.discount_code import DiscountCode

# ---- Admin schemas ----


class DiscountCodeSchema(ModelSchema):
    """Response schema for discount codes (admin)."""

    discount_type: DiscountCode.DiscountType
    series_ids: list[UUID] = []
    event_ids: list[UUID] = []
    tier_ids: list[UUID] = []

    class Meta:
        model = DiscountCode
        fields = [
            "id",
            "code",
            "discount_type",
            "discount_value",
            "currency",
            "valid_from",
            "valid_until",
            "max_uses",
            "max_uses_per_user",
            "times_used",
            "min_purchase_amount",
            "is_active",
            "created_at",
        ]

    @staticmethod
    def resolve_series_ids(obj: DiscountCode) -> list[UUID]:
        """Resolve M2M series to list of IDs."""
        return list(obj.series.values_list("id", flat=True))

    @staticmethod
    def resolve_event_ids(obj: DiscountCode) -> list[UUID]:
        """Resolve M2M events to list of IDs."""
        return list(obj.events.values_list("id", flat=True))

    @staticmethod
    def resolve_tier_ids(obj: DiscountCode) -> list[UUID]:
        """Resolve M2M tiers to list of IDs."""
        return list(obj.tiers.values_list("id", flat=True))


class _DiscountCodeValidatorMixin:
    """Shared validation logic for create and update schemas."""

    discount_type: DiscountCode.DiscountType | None
    discount_value: Decimal | None
    currency: str | None
    valid_from: AwareDatetime | None
    valid_until: AwareDatetime | None

    @model_validator(mode="after")
    def validate_discount_fields(self) -> t.Self:
        """Validate cross-field constraints."""
        # Percentage must not exceed 100
        if (
            self.discount_type == DiscountCode.DiscountType.PERCENTAGE
            and self.discount_value is not None
            and self.discount_value > Decimal("100")
        ):
            raise ValueError("Percentage discount cannot exceed 100.")

        # Currency required for fixed amount
        if self.discount_type == DiscountCode.DiscountType.FIXED_AMOUNT and not self.currency:
            raise ValueError("Currency is required for fixed amount discounts.")

        # Date ordering
        if self.valid_from and self.valid_until and self.valid_from >= self.valid_until:
            raise ValueError("valid_until must be after valid_from.")

        return self


class DiscountCodeCreateSchema(_DiscountCodeValidatorMixin, Schema):
    """Schema for creating a discount code."""

    code: str = Field(..., max_length=64, min_length=1)
    discount_type: DiscountCode.DiscountType
    discount_value: Decimal = Field(..., ge=0)
    currency: str | None = Field(None, max_length=3)
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    max_uses: int | None = Field(None, ge=1)
    max_uses_per_user: int = Field(1, ge=1)
    min_purchase_amount: Decimal = Field(Decimal("0"), ge=0)
    is_active: bool = True
    series_ids: list[UUID] | None = None
    event_ids: list[UUID] | None = None
    tier_ids: list[UUID] | None = None


class DiscountCodeUpdateSchema(_DiscountCodeValidatorMixin, Schema):
    """Schema for updating a discount code. Code cannot be changed after creation."""

    discount_type: DiscountCode.DiscountType | None = None
    discount_value: Decimal | None = Field(None, ge=0)
    currency: str | None = Field(None, max_length=3)
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    max_uses: int | None = Field(None, ge=1)
    max_uses_per_user: int | None = Field(None, ge=1)
    min_purchase_amount: Decimal | None = Field(None, ge=0)
    is_active: bool | None = None
    series_ids: list[UUID] | None = None
    event_ids: list[UUID] | None = None
    tier_ids: list[UUID] | None = None


# ---- Public schemas ----


class DiscountCodeValidationSchema(Schema):
    """Request schema for validating a discount code at checkout."""

    code: str = Field(..., min_length=1, max_length=64)


class DiscountCodeValidationResponse(Schema):
    """Response schema for discount code validation preview."""

    valid: bool
    discount_type: DiscountCode.DiscountType | None = None
    discount_value: Decimal | None = None
    discounted_price: Decimal | None = None
    message: str | None = None
