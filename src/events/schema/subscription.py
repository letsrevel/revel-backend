"""Subscription, plan, and payment schemas (Phase 1)."""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    OrganizationMember,
    SubscriptionPaymentMethod,
)

from .mixins import get_image_field_url
from .organization import MembershipTierSchema
from .ticket import Currencies


class PlanSchema(ModelSchema):
    """Response schema for a subscription plan (staff-facing)."""

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
            "is_active",
            "max_subscriptions",
        ]

    @staticmethod
    def resolve_active_subscription_count(obj: MembershipSubscriptionPlan) -> int:
        """Non-terminal subscriptions currently occupying cap slots.

        Reads the ``active_subscription_count`` annotation when present (set by
        ``MembershipSubscriptionPlan.objects.with_active_subscription_count()``
        on list querysets — avoids one COUNT per row). Falls back to a direct
        COUNT for single-object callers that haven't annotated.
        """
        annotated = getattr(obj, "active_subscription_count", None)
        if annotated is not None:
            return int(annotated)
        return obj.subscriptions.exclude(status__in=MembershipSubscription.TERMINAL_STATUSES).count()

    @staticmethod
    def resolve_tier_name(obj: MembershipSubscriptionPlan) -> str:
        """Return the parent tier's display name."""
        return obj.tier.name


class PublicPlanSchema(ModelSchema):
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
            taken = obj.subscriptions.exclude(status__in=MembershipSubscription.TERMINAL_STATUSES).count()
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

    @staticmethod
    def resolve_tier_name(obj: MembershipSubscriptionPlan) -> str:
        """Return the parent tier's display name."""
        return obj.tier.name


class PlanCreateSchema(Schema):
    """Create payload for a subscription plan (tier inferred from URL)."""

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


class PlanUpdateSchema(Schema):
    """Partial update payload for a subscription plan.

    ``payment_method`` is intentionally not patchable: switching between
    OFFLINE and ONLINE mid-lifecycle would require non-trivial Stripe
    migration. Archive the plan and create a new one instead.
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


class SubscriptionCreateSchema(Schema):
    """Create payload for an OFFLINE-managed subscription."""

    plan_id: UUID
    user_id: UUID
    initial_payment_amount: Decimal | None = Field(None, ge=Decimal("0"))
    initial_payment_currency: Currencies | None = None
    initial_payment_notes: str = ""

    @model_validator(mode="after")
    def _validate_initial_payment(self) -> "SubscriptionCreateSchema":
        """Require ``initial_payment_currency`` when ``initial_payment_amount`` is set."""
        if self.initial_payment_amount is not None and not self.initial_payment_currency:
            raise ValueError("initial_payment_currency is required when initial_payment_amount is set.")
        return self


class CancelSubscriptionSchema(Schema):
    """Cancel-subscription payload."""

    immediate: bool = False


class PaymentRecordSchema(Schema):
    """Payload to manually record an OFFLINE payment against a subscription."""

    amount: Decimal = Field(..., ge=Decimal("0"))
    currency: Currencies
    status: MembershipPayment.PaymentStatus = MembershipPayment.PaymentStatus.SUCCEEDED
    notes: str = ""
    occurred_at: AwareDatetime | None = Field(
        None,
        description=(
            "Override the payment date for backfills. Anchors period_start/period_end math; "
            "defaults to now when omitted."
        ),
    )


class RefundSchema(Schema):
    """Payload for refunding a recorded payment (record-only in MVP)."""

    notes: str = ""


class PaymentSchema(ModelSchema):
    """Response schema for a membership payment."""

    subscription_id: UUID
    status: MembershipPayment.PaymentStatus
    period_start: AwareDatetime
    period_end: AwareDatetime
    occurred_at: AwareDatetime | None = None
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
    pending_plan_id: UUID | None = None
    expired_at: AwareDatetime | None = None
    revival_deadline: AwareDatetime | None = None

    class Meta:
        model = MembershipSubscription
        fields = [
            "id",
            "cancel_at_period_end",
            "created_at",
            "updated_at",
        ]

    @staticmethod
    def resolve_revival_deadline(obj: MembershipSubscription) -> datetime | None:
        """Deadline to revive an EXPIRED subscription in place.

        ``expired_at + org.membership_subscription_revival_window_days``. Returns ``None``
        unless the subscription is EXPIRED, has an ``expired_at`` timestamp, and the org's
        revival window is greater than zero — mirroring ``_validate_revivable`` in
        ``subscription_service`` so the surfaced deadline matches what revival enforces.
        """
        if obj.status != MembershipSubscription.SubscriptionStatus.EXPIRED or obj.expired_at is None:
            return None
        window = obj.organization.membership_subscription_revival_window_days
        if window <= 0:
            return None
        return obj.expired_at + timedelta(days=window)


class MySubscriptionSchema(_BaseSubscriptionSchema):
    """Member-facing view of their own subscription (no PII about other users)."""

    plan: MemberPlanSchema
    organization_name: str
    organization_slug: str
    organization_logo_url: str | None = None

    @staticmethod
    def resolve_plan(obj: MembershipSubscription) -> MembershipSubscriptionPlan:
        """Return the plan for nested serialization."""
        return obj.plan

    @staticmethod
    def resolve_organization_name(obj: MembershipSubscription) -> str:
        """Return the parent organization's name."""
        return obj.organization.name

    @staticmethod
    def resolve_organization_slug(obj: MembershipSubscription) -> str:
        """Return the parent organization's slug."""
        return obj.organization.slug

    @staticmethod
    def resolve_organization_logo_url(obj: MembershipSubscription) -> str | None:
        """Return the parent organization's logo thumbnail URL, if any."""
        return get_image_field_url(obj.organization, "logo_thumbnail")


class MyMembershipSchema(Schema):
    """Member-facing view of a single org membership, with optional inlined active subscription.

    Surfaces both legacy memberships (no subscription) and subscription-backed memberships
    in a single shape.
    """

    organization_id: UUID
    organization_name: str
    organization_slug: str
    organization_logo_url: str | None = None
    member_since: AwareDatetime = Field(alias="created_at")
    status: OrganizationMember.MembershipStatus
    tier: MembershipTierSchema | None = None
    subscription: MySubscriptionSchema | None = None

    @staticmethod
    def resolve_organization_id(obj: OrganizationMember) -> UUID:
        """Return the organization's UUID."""
        return obj.organization_id

    @staticmethod
    def resolve_organization_name(obj: OrganizationMember) -> str:
        """Return the organization's name."""
        return obj.organization.name

    @staticmethod
    def resolve_organization_slug(obj: OrganizationMember) -> str:
        """Return the organization's slug."""
        return obj.organization.slug

    @staticmethod
    def resolve_organization_logo_url(obj: OrganizationMember) -> str | None:
        """Return the organization's logo thumbnail URL, if any."""
        return get_image_field_url(obj.organization, "logo_thumbnail")

    @staticmethod
    def resolve_subscription(obj: OrganizationMember) -> MembershipSubscription | None:
        """Return the caller's active (non-terminal) subscription for this organization, if any."""
        subs: list[MembershipSubscription] = getattr(obj.organization, "_caller_active_subs", [])
        return subs[0] if subs else None


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


class SubscribeRequestSchema(Schema):
    """Member-initiated subscribe payload."""

    plan_id: UUID


class SubscribeResponseSchema(Schema):
    """Response to a member-initiated subscribe.

    Carries the hosted Stripe Checkout ``checkout_url`` the frontend
    redirects the member to for payment.
    """

    subscription: MySubscriptionSchema
    checkout_url: str


class MemberCancelSubscriptionSchema(Schema):
    """Member-initiated cancel payload."""

    immediate: bool = False


class ChangePlanRequestSchema(Schema):
    """Member-initiated change-plan payload.

    Server decides upgrade vs. downgrade based on price delta and routes to
    Stripe accordingly. Currency must match the current plan's.
    """

    plan_id: UUID


class BillingPortalRequestSchema(Schema):
    """Member-initiated billing-portal session request.

    ``return_url`` is the URL Stripe redirects to when the user closes the
    portal. Validated as a real http(s) URL so we don't hand Stripe a
    ``javascript:`` / ``data:`` / malformed redirect target. Defaults to the
    platform's frontend base URL when omitted.
    """

    return_url: HttpUrl | None = Field(None, max_length=2000)


class BillingPortalSessionSchema(Schema):
    """Response payload for the billing-portal endpoint."""

    url: str


class RevivalRequestSchema(Schema):
    """Body for revival endpoints.

    For OFFLINE revival, provide amount + currency (+ optional notes).
    For ONLINE revival, send an empty body — the endpoint returns a hosted
    Stripe Checkout URL that collects the new Stripe Subscription's first
    payment.
    """

    amount: Decimal | None = None
    currency: Currencies | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _validate_amount_currency_pair(self) -> "RevivalRequestSchema":
        if (self.amount is None) != (self.currency is None):
            raise ValueError("amount and currency must be provided together.")
        return self


class RevivalResponseSchema(Schema):
    """Response from a successful member-initiated revival call.

    For OFFLINE revival, ``checkout_url`` is ``None``.
    For ONLINE revival, ``checkout_url`` is the hosted Stripe Checkout URL
    the member must complete to finish the renewal.
    """

    subscription: MySubscriptionSchema
    checkout_url: str | None = None


class StaffRevivalResponseSchema(Schema):
    """Response from a successful staff-initiated revival call.

    Carries the admin-facing subscription view (includes user PII fields).
    For OFFLINE revival, ``checkout_url`` is ``None``.
    For ONLINE revival, ``checkout_url`` is the hosted Stripe Checkout URL
    the member must complete — the member is also emailed the link, and the
    URL is returned so staff can hand it over out-of-band.
    """

    subscription: SubscriptionSchema
    checkout_url: str | None = None


class MigrationAcceptedSchema(Schema):
    """Acknowledgement that a force-migrate-subscribers job was queued.

    The migration runs asynchronously (one Stripe call per ONLINE subscriber),
    so the endpoint returns immediately with the number of non-terminal
    subscribers targeted; per-subscriber outcomes are recorded in worker logs.
    """

    queued: int


class SubscriptionStatusBreakdownSchema(Schema):
    """Per-status count breakdown for subscription metrics."""

    pending: int
    active: int
    paused: int
    past_due: int
    cancelled: int
    expired: int


class SubscriptionMetricsSchema(Schema):
    """Aggregated subscription metrics for an organization."""

    as_of: AwareDatetime
    active_count: int
    mrr: Decimal
    mrr_currency: str
    mixed_currency_warning: bool
    new_subscribers_30d: int
    churned_30d: int
    churn_rate_30d: float
    status_breakdown: SubscriptionStatusBreakdownSchema
