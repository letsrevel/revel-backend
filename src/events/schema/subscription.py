"""Subscription, plan, and payment schemas (Phase 1)."""

from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, Field

from events.models import MembershipPayment, MembershipSubscription, MembershipSubscriptionPlan

from .ticket import Currencies


class PlanSchema(ModelSchema):
    """Response schema for a subscription plan."""

    tier_id: UUID
    period_unit: MembershipSubscriptionPlan.PeriodUnit

    class Meta:
        model = MembershipSubscriptionPlan
        fields = [
            "id",
            "name",
            "description",
            "price",
            "currency",
            "period_count",
            "is_active",
        ]


class PlanCreateSchema(Schema):
    """Create payload for a subscription plan (tier inferred from URL)."""

    name: str = Field(..., max_length=255)
    description: str = ""
    price: Decimal = Field(..., ge=Decimal("0"))
    currency: Currencies
    period_unit: MembershipSubscriptionPlan.PeriodUnit = MembershipSubscriptionPlan.PeriodUnit.MONTH
    period_count: int = Field(1, ge=1, le=120)
    is_active: bool = True


class PlanUpdateSchema(Schema):
    """Partial update payload for a subscription plan."""

    name: str | None = Field(None, max_length=255)
    description: str | None = None
    price: Decimal | None = Field(None, ge=Decimal("0"))
    currency: Currencies | None = None
    period_unit: MembershipSubscriptionPlan.PeriodUnit | None = None
    period_count: int | None = Field(None, ge=1, le=120)
    is_active: bool | None = None


class SubscriptionCreateSchema(Schema):
    """Create payload for an OFFLINE-managed subscription."""

    plan_id: UUID
    user_id: UUID
    initial_payment_amount: Decimal | None = Field(None, ge=Decimal("0"))
    initial_payment_currency: Currencies | None = None
    initial_payment_notes: str = ""


class CancelSubscriptionSchema(Schema):
    """Cancel-subscription payload."""

    immediate: bool = False


class PaymentRecordSchema(Schema):
    """Payload to manually record an OFFLINE payment against a subscription."""

    amount: Decimal = Field(..., ge=Decimal("0"))
    currency: Currencies
    status: MembershipPayment.PaymentStatus = MembershipPayment.PaymentStatus.SUCCEEDED
    notes: str = ""


class RefundSchema(Schema):
    """Payload for refunding a recorded payment (record-only in MVP)."""

    notes: str = ""


class PaymentSchema(ModelSchema):
    """Response schema for a membership payment."""

    subscription_id: UUID
    status: MembershipPayment.PaymentStatus
    period_start: AwareDatetime
    period_end: AwareDatetime
    recorded_by_id: UUID | None = None
    recorded_by_name: str | None = None

    class Meta:
        model = MembershipPayment
        fields = [
            "id",
            "amount",
            "currency",
            "notes",
            "created_at",
        ]

    @staticmethod
    def resolve_recorded_by_id(obj: MembershipPayment) -> UUID | None:
        """Return the recorder's user ID."""
        return obj.recorded_by_id

    @staticmethod
    def resolve_recorded_by_name(obj: MembershipPayment) -> str | None:
        """Return the display name of the recording staff user."""
        if obj.recorded_by:
            return obj.recorded_by.get_display_name()
        return None


class _BaseSubscriptionSchema(ModelSchema):
    plan_id: UUID
    organization_id: UUID
    status: MembershipSubscription.SubscriptionStatus
    current_period_start: AwareDatetime | None = None
    current_period_end: AwareDatetime | None = None
    cancelled_at: AwareDatetime | None = None

    class Meta:
        model = MembershipSubscription
        fields = [
            "id",
            "cancel_at_period_end",
            "created_at",
            "updated_at",
        ]


class MySubscriptionSchema(_BaseSubscriptionSchema):
    """Member-facing view of their own subscription (no PII about other users)."""

    plan: PlanSchema

    @staticmethod
    def resolve_plan(obj: MembershipSubscription) -> MembershipSubscriptionPlan:
        """Return the plan for nested serialization."""
        return obj.plan


class SubscriptionSchema(_BaseSubscriptionSchema):
    """Admin-facing view: includes the member's user id + display name."""

    user_id: UUID
    user_display_name: str
    user_email: str
    plan: PlanSchema

    @staticmethod
    def resolve_user_display_name(obj: MembershipSubscription) -> str:
        """Display name of the subscriber."""
        return obj.user.get_display_name()

    @staticmethod
    def resolve_user_email(obj: MembershipSubscription) -> str:
        """Email of the subscriber."""
        return obj.user.email

    @staticmethod
    def resolve_plan(obj: MembershipSubscription) -> MembershipSubscriptionPlan:
        """Return the plan for nested serialization."""
        return obj.plan
