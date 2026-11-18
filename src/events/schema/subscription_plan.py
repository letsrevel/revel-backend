"""Subscription plan schemas (staff, public and member-facing views, create/update payloads)."""

from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, Field, model_validator

from events.models import MembershipSubscriptionPlan, SubscriptionPaymentMethod
from events.utils.subscription_plan_rules import validate_plan_shape

from .ticket import Currencies


class FirstPeriodFieldsMixin(Schema):
    """Calendar-alignment fields plus the "if a member joined now" first-period quote.

    Not for the schema nested under a member's own subscription: there the quote
    is misleading, so :class:`MemberPlanSchema` declares only the static fields.
    """

    period_alignment: MembershipSubscriptionPlan.PeriodAlignment
    first_period_pricing: MembershipSubscriptionPlan.FirstPeriodPricing
    first_period_end: AwareDatetime | None = None
    first_period_price: Decimal | None = None

    @staticmethod
    def resolve_first_period_end(obj: MembershipSubscriptionPlan) -> AwareDatetime | None:
        """End of the first period if a member joined now (calendar plans only)."""
        return obj.first_period_quote[0]

    @staticmethod
    def resolve_first_period_price(obj: MembershipSubscriptionPlan) -> Decimal | None:
        """Price of the first period if a member joined now (calendar plans only)."""
        return obj.first_period_quote[1]


class PlanSchema(FirstPeriodFieldsMixin, ModelSchema):
    """Response schema for a subscription plan (staff-facing).

    Exposes the plan's Stripe Product/Price ids so organizers can locate the
    objects backing an ONLINE plan in their Stripe Dashboard (both are empty
    strings for OFFLINE plans).
    """

    tier_id: UUID
    tier_name: str
    period_unit: MembershipSubscriptionPlan.PeriodUnit
    payment_method: SubscriptionPaymentMethod
    sales_status: MembershipSubscriptionPlan.SalesStatus
    active_subscription_count: int

    class Meta:
        model = MembershipSubscriptionPlan
        fields = [
            "id",
            "name",
            "description",
            "price",
            "currency",
            "period_count",
            "calendar_start_month",
            "is_active",
            "max_subscriptions",
            "payment_instructions",
            "stripe_product_id",
            "stripe_price_id",
        ]

    @staticmethod
    def resolve_active_subscription_count(obj: MembershipSubscriptionPlan) -> int:
        """Non-terminal subscriptions currently occupying (or reserving) cap slots.

        Reads the ``active_subscription_count`` annotation when present (set by
        ``MembershipSubscriptionPlan.objects.with_active_subscription_count()``
        on list querysets — avoids one COUNT per row). Falls back to
        ``occupied_slot_count()`` for single-object callers that haven't
        annotated.
        """
        annotated = getattr(obj, "active_subscription_count", None)
        if annotated is not None:
            return int(annotated)
        return obj.occupied_slot_count()

    @staticmethod
    def resolve_tier_name(obj: MembershipSubscriptionPlan) -> str:
        """Return the parent tier's display name."""
        return obj.tier.name


class PublicPlanSchema(FirstPeriodFieldsMixin, ModelSchema):
    """Response schema for a subscription plan (public/member-facing).

    Mirrors :class:`PlanSchema` but only exposes archived plans hidden — the
    public list filters them — and does not return Stripe internals.
    """

    tier_id: UUID
    tier_name: str
    period_unit: MembershipSubscriptionPlan.PeriodUnit
    payment_method: SubscriptionPaymentMethod
    sales_status: MembershipSubscriptionPlan.SalesStatus
    sold_out: bool

    class Meta:
        model = MembershipSubscriptionPlan
        fields = [
            "id",
            "name",
            "description",
            "price",
            "currency",
            "period_count",
            "calendar_start_month",
        ]

    @staticmethod
    def resolve_sold_out(obj: MembershipSubscriptionPlan) -> bool:
        """True when the plan's subscription cap is fully occupied.

        Lets the frontend distinguish "sold out" (cap reached) from
        "sales paused" (``sales_status``) when rendering the join CTA.
        Reads the ``active_subscription_count`` annotation when present
        (see ``with_active_subscription_count()``); falls back to a COUNT.
        """
        if obj.max_subscriptions is None:
            return False
        taken = getattr(obj, "active_subscription_count", None)
        if taken is None:
            taken = obj.occupied_slot_count()
        return int(taken) >= obj.max_subscriptions

    @staticmethod
    def resolve_tier_name(obj: MembershipSubscriptionPlan) -> str:
        """Return the parent tier's display name."""
        return obj.tier.name


class MemberPlanSchema(ModelSchema):
    """Member-facing view of a plan, nested under the member's own subscription.

    Mirrors :class:`PlanSchema` minus the organizer sale-control/capacity data
    (``max_subscriptions``, ``active_subscription_count``) — occupancy
    telemetry is staff-facing and must not leak through the ``/me`` surface.
    """

    tier_id: UUID
    tier_name: str
    period_unit: MembershipSubscriptionPlan.PeriodUnit
    payment_method: SubscriptionPaymentMethod
    sales_status: MembershipSubscriptionPlan.SalesStatus

    period_alignment: MembershipSubscriptionPlan.PeriodAlignment
    first_period_pricing: MembershipSubscriptionPlan.FirstPeriodPricing

    class Meta:
        model = MembershipSubscriptionPlan
        fields = [
            "id",
            "name",
            "description",
            "price",
            "currency",
            "period_count",
            "calendar_start_month",
            "is_active",
        ]

    @staticmethod
    def resolve_tier_name(obj: MembershipSubscriptionPlan) -> str:
        """Return the parent tier's display name."""
        return obj.tier.name


class PlanCreateSchema(Schema):
    """Create payload for a subscription plan (tier inferred from URL).

    The (payment method, price, cadence) triple must be coherent — see
    :func:`events.utils.subscription_plan_rules.validate_plan_shape`, which is
    also what ``PATCH`` enforces so the two can never drift.
    """

    name: str = Field(..., max_length=255)
    description: str = ""
    price: Decimal = Field(..., ge=Decimal("0"))
    currency: Currencies
    period_unit: MembershipSubscriptionPlan.PeriodUnit = MembershipSubscriptionPlan.PeriodUnit.MONTH
    period_count: int = Field(1, ge=1, le=120)
    is_active: bool = True
    payment_method: SubscriptionPaymentMethod = MembershipSubscriptionPlan.PaymentMethod.OFFLINE
    sales_status: MembershipSubscriptionPlan.SalesStatus = MembershipSubscriptionPlan.SalesStatus.OPEN
    max_subscriptions: int | None = Field(default=None, ge=1)
    payment_instructions: str | None = None
    period_alignment: MembershipSubscriptionPlan.PeriodAlignment = MembershipSubscriptionPlan.PeriodAlignment.ROLLING
    calendar_start_month: int = Field(1, ge=1, le=12)
    first_period_pricing: MembershipSubscriptionPlan.FirstPeriodPricing = (
        MembershipSubscriptionPlan.FirstPeriodPricing.PRORATED
    )

    @model_validator(mode="after")
    def _validate_plan_shape(self) -> "PlanCreateSchema":
        """Reject free-but-priced, online-but-free, and cadence/method mismatches."""
        message = validate_plan_shape(
            payment_method=self.payment_method,
            price=self.price,
            period_unit=self.period_unit,
            period_alignment=self.period_alignment,
            period_count=self.period_count,
            calendar_start_month=self.calendar_start_month,
            first_period_pricing=self.first_period_pricing,
        )
        if message:
            raise ValueError(message)
        return self


class PlanUpdateSchema(Schema):
    """Partial update payload for a subscription plan.

    ``period_alignment`` and ``calendar_start_month`` are not patchable either
    (like ``payment_method``): re-aligning a plan would shift every subscriber's
    billing boundaries. ``first_period_pricing`` only affects future joiners.

    ``payment_method`` is intentionally not patchable: switching between
    OFFLINE, ONLINE and FREE mid-lifecycle would require non-trivial Stripe
    migration (and, for FREE, refunding nobody). Archive the plan and create a
    new one instead.

    The price/cadence rules bound to the plan's *existing* payment method are
    enforced by ``subscription.plans.update_plan``, which is the only place
    that can see the merged post-patch shape.
    """

    name: str | None = Field(None, max_length=255)
    description: str | None = None
    price: Decimal | None = Field(None, ge=Decimal("0"))
    currency: Currencies | None = None
    period_unit: MembershipSubscriptionPlan.PeriodUnit | None = None
    period_count: int | None = Field(None, ge=1, le=120)
    is_active: bool | None = None
    sales_status: MembershipSubscriptionPlan.SalesStatus | None = Field(default=None)
    max_subscriptions: int | None = Field(default=None, ge=1)
    payment_instructions: str | None = None
    first_period_pricing: MembershipSubscriptionPlan.FirstPeriodPricing | None = None
