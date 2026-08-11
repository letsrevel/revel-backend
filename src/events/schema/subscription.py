"""Subscription, plan, and payment schemas (Phase 1)."""

import typing as t
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from accounts.schema import MemberUserSchema
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    OrganizationMember,
    SubscriptionPaymentMethod,
)
from events.service.membership_manager.resolvers import (
    resolve_membership_questionnaire,
    resolve_requires_membership_approval,
)
from events.utils.subscription_plan_rules import validate_plan_shape

from .mixins import get_image_field_url
from .organization import MembershipTierSchema
from .ticket import Currencies


class PlanSchema(ModelSchema):
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
            "is_active",
            "max_subscriptions",
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
            taken = obj.occupied_slot_count()
        return int(taken) >= obj.max_subscriptions

    @staticmethod
    def resolve_tier_name(obj: MembershipSubscriptionPlan) -> str:
        """Return the parent tier's display name."""
        return obj.tier.name


class PublicMembershipTierSchema(ModelSchema):
    """Public/member-facing view of a membership tier and how it can be joined.

    Lives here rather than in ``schema/organization.py`` because that module is
    imported by this one; nesting :class:`PublicPlanSchema` there would be a
    circular import (same reason as :class:`OrganizationMemberSchema`).

    The policy fields are *resolved*, not raw: ``requires_approval`` and
    ``questionnaire_id`` already fold the tier-level override into the organization
    default, so the frontend renders the join flow without re-implementing the
    inheritance rules. Resolution reads ``tier.organization`` and
    ``tier.membership_questionnaire``, so the listing queryset must
    ``select_related("membership_questionnaire", "organization__default_membership_questionnaire")``.
    """

    description: str | None = None
    requires_approval: bool
    questionnaire_id: UUID | None = Field(
        default=None,
        description=(
            "PK of the underlying Questionnaire (not of the OrganizationQuestionnaire wrapper), "
            "so it can be passed straight to "
            "GET /me/organizations/{slug}/membership-questionnaire/{questionnaire_id}. "
            "Matches MembershipEligibilitySchema.questionnaire_id."
        ),
    )
    plans: list[PublicPlanSchema]
    is_free: bool = Field(
        description="True when the tier has no active plan, i.e. it is joined through the free /apply path."
    )

    class Meta:
        model = MembershipTier
        fields = ["id", "name", "description", "display_order"]

    @staticmethod
    def resolve_requires_approval(obj: MembershipTier) -> bool:
        """Whether joining this tier needs manual staff approval (tier override, else org default)."""
        return resolve_requires_membership_approval(obj.organization, obj)

    @staticmethod
    def resolve_questionnaire_id(obj: MembershipTier) -> UUID | None:
        """PK of the ``Questionnaire`` behind the applicable ``OrganizationQuestionnaire``, if any."""
        oq = resolve_membership_questionnaire(obj.organization, obj)
        return oq.questionnaire_id if oq is not None else None

    @staticmethod
    def resolve_plans(obj: MembershipTier) -> list[MembershipSubscriptionPlan]:
        """The tier's active plans, from the listing's ``Prefetch(..., to_attr="active_plans")``.

        Returned as model instances so django-ninja runs :class:`PublicPlanSchema`'s own
        resolvers (``sold_out``, ``tier_name``) against them. A tier without the prefetch
        (single-object callers) is reported as having none rather than firing a query.
        """
        plans: list[MembershipSubscriptionPlan] = getattr(obj, "active_plans", [])
        return plans

    @staticmethod
    def resolve_is_free(obj: MembershipTier) -> bool:
        """True when no active plan gates the tier, so the free ``/apply`` path applies."""
        return not getattr(obj, "active_plans", [])


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

    @model_validator(mode="after")
    def _validate_plan_shape(self) -> "PlanCreateSchema":
        """Reject free-but-priced, online-but-free, and cadence/method mismatches."""
        message = validate_plan_shape(
            payment_method=self.payment_method,
            price=self.price,
            period_unit=self.period_unit,
        )
        if message:
            raise ValueError(message)
        return self


class PlanUpdateSchema(Schema):
    """Partial update payload for a subscription plan.

    ``payment_method`` is intentionally not patchable: switching between
    OFFLINE, ONLINE and FREE mid-lifecycle would require non-trivial Stripe
    migration (and, for FREE, refunding nobody). Archive the plan and create a
    new one instead.

    The price/cadence rules bound to the plan's *existing* payment method are
    enforced by ``subscription_service.update_plan``, which is the only place
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


class SubscriptionCreateSchema(Schema):
    """Staff create payload for a subscription on an OFFLINE or FREE plan.

    ONLINE plans are refused — the member must subscribe themselves so they can
    confirm the first payment. The ``initial_payment_*`` fields are for the
    OFFLINE case only: a FREE plan has nothing to collect, so supplying them
    with one is refused too.
    """

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
    """Payload to manually record a payment against an OFFLINE subscription.

    Hand-recorded payments are OFFLINE-only: ONLINE rows are settled by the
    ``invoice.paid`` webhook (hand-recording would duplicate them) and FREE
    rows never collect anything.
    """

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


# Named distinctly from the ticket ``PaymentSchema`` on purpose: django-ninja
# derives OpenAPI component names from the bare class name and disambiguates a
# clash by appending a counter. That counter follows import-encounter order, so
# a future schema addition could silently swap which class owns the bare name —
# re-pointing an existing type in the generated frontend client. Keep the names
# distinct rather than relying on the alias in ``events/schema/__init__.py``,
# which renames only the Python import and not ``cls.__name__``.
class MembershipPaymentSchema(ModelSchema):
    """Response schema for a membership payment (staff-facing).

    ``stripe_dashboard_url`` mirrors the ticket admin surface: a clickable
    "manage on Stripe" link for ONLINE payments (``None`` for OFFLINE ones).

    The platform fee is **never reduced by refunds**: Revel keeps its fee when a
    payment is refunded (mirroring Stripe keeping its processing fee — nothing
    ever sets ``refund_application_fee``), so the ``platform_fee*``
    decomposition always reflects the original charge. Frontends can state this
    affirmatively rather than hedge.
    """

    subscription_id: UUID
    status: MembershipPayment.PaymentStatus
    period_start: AwareDatetime
    period_end: AwareDatetime
    occurred_at: AwareDatetime | None = None
    recorded_by_id: UUID | None = None
    recorded_by_name: str | None = None
    stripe_dashboard_url: str | None = None

    class Meta:
        model = MembershipPayment
        fields = [
            "id",
            "amount",
            "currency",
            "notes",
            "created_at",
            "stripe_invoice_id",
            "stripe_payment_intent_id",
            # Platform-fee decomposition, mirroring the ticket ``PaymentSchema``:
            # ``amount`` alone is gross, so without these an organizer cannot
            # reconcile a payout or account for the fee's VAT.
            "platform_fee",
            "platform_fee_net",
            "platform_fee_vat",
            "platform_fee_vat_rate",
            "platform_fee_reverse_charge",
            # Refund audit trail: a partial refund leaves ``status`` SUCCEEDED,
            # so without these an organizer cannot tell it happened at all.
            "refund_amount",
            "refunded_at",
            "stripe_refund_id",
        ]

    @staticmethod
    def resolve_stripe_dashboard_url(obj: MembershipPayment) -> str | None:
        """Stripe Dashboard link for this payment (None for OFFLINE/manual rows)."""
        return obj.stripe_dashboard_url()

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


class MyMembershipPaymentSchema(ModelSchema):
    """Member-facing view of one of the caller's own membership payments.

    Deliberately *not* a subclass of :class:`MembershipPaymentSchema`: this is
    the receipt a member sees, so it carries only what they paid and for which
    period. No ``raw_response``, no platform-fee decomposition, no Stripe ids,
    no staff ``notes`` / ``recorded_by`` — those are organizer-facing.
    """

    status: MembershipPayment.PaymentStatus
    period_start: AwareDatetime
    period_end: AwareDatetime

    class Meta:
        model = MembershipPayment
        fields = [
            "id",
            "amount",
            "currency",
            "created_at",
            # A partial refund leaves ``status`` SUCCEEDED, so without these the
            # member cannot tell part of their money came back.
            "refund_amount",
            "refunded_at",
        ]


class OrganizationMembershipPaymentSchema(MembershipPaymentSchema):
    """Org-wide payment ledger row: adds the member and plan identity.

    The per-subscription listing already knows who and which plan it belongs to;
    the org-wide reconciliation listing does not, so each row carries it (flat
    fields, mirroring :class:`SubscriptionSchema`'s member identity).
    """

    user_id: UUID
    user_email: str
    user_display_name: str
    plan_id: UUID
    plan_name: str
    payment_method: SubscriptionPaymentMethod

    @staticmethod
    def resolve_user_id(obj: MembershipPayment) -> UUID:
        """Return the subscriber's user id."""
        return obj.subscription.user_id

    @staticmethod
    def resolve_user_email(obj: MembershipPayment) -> str:
        """Return the subscriber's email."""
        return obj.subscription.user.email

    @staticmethod
    def resolve_user_display_name(obj: MembershipPayment) -> str:
        """Return the subscriber's display name."""
        return obj.subscription.user.get_display_name()

    @staticmethod
    def resolve_plan_id(obj: MembershipPayment) -> UUID:
        """Return the id of the plan billed."""
        return obj.subscription.plan_id

    @staticmethod
    def resolve_plan_name(obj: MembershipPayment) -> str:
        """Return the name of the plan billed."""
        return obj.subscription.plan.name

    @staticmethod
    def resolve_payment_method(obj: MembershipPayment) -> str:
        """Payment method of the plan billed.

        Lets the ledger distinguish ONLINE rows (refunds must go through
        Stripe — the in-app refund endpoint 400s) from OFFLINE rows (where the
        in-app refund is the correct action). The ledger queryset
        ``select_related``s ``subscription__plan``, so this is join-backed.
        """
        return obj.subscription.plan.payment_method


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
    grace_deadline: AwareDatetime | None = None

    @staticmethod
    def resolve_grace_deadline(obj: MembershipSubscription) -> datetime | None:
        """Deadline to settle a failed payment before the membership expires.

        ``current_period_end + org.membership_grace_period_days`` — the same arithmetic
        the grace-expiry sweep in ``events.tasks.subscriptions`` uses to flip PAST_DUE
        rows to EXPIRED. Returns ``None`` unless the subscription is PAST_DUE and has a
        ``current_period_end``.
        """
        if obj.status != MembershipSubscription.SubscriptionStatus.PAST_DUE or obj.current_period_end is None:
            return None
        return obj.current_period_end + timedelta(days=obj.organization.membership_grace_period_days)

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
    apple_pass_available: bool
    google_pass_available: bool

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
    """Admin-facing view: includes the member's user id + display name.

    Also exposes the Stripe handles (subscription/checkout-session/schedule ids
    and a Dashboard link) so organizers can manage ONLINE subscriptions on
    Stripe — parity with the ticket admin surface. The Checkout Session id is
    what makes a PENDING (not-yet-linked) subscription inspectable.
    """

    user_id: UUID
    user_display_name: str
    user_email: str
    plan: PlanSchema
    member_status: OrganizationMember.MembershipStatus | None = None
    stripe_subscription_id: str | None = None
    stripe_checkout_session_id: str = ""
    stripe_schedule_id: str = ""
    stripe_dashboard_url: str | None = None

    @staticmethod
    def resolve_stripe_dashboard_url(obj: MembershipSubscription) -> str | None:
        """Stripe Dashboard link: the Subscription, else its Checkout Session (None when OFFLINE)."""
        return obj.stripe_dashboard_url()

    @staticmethod
    def resolve_member_status(obj: MembershipSubscription) -> str | None:
        """Membership status of the subscriber's member row (``None`` when no row exists).

        Lets the admin drawer pre-gate actions that 403 for PAUSED/BANNED
        members (e.g. uncancel) instead of translating the error after the
        fact. List endpoints annotate ``member_status`` onto the queryset
        (one correlated subquery, no N+1); single-object responses fall back
        to one lookup.
        """
        annotated = getattr(obj, "member_status", None)
        if annotated is not None:
            return t.cast(str, annotated)
        return (
            OrganizationMember.objects.filter(
                organization_id=obj.organization_id,
                user_id=obj.user_id,
            )
            .values_list("status", flat=True)
            .first()
        )

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


class OrganizationMemberSchema(Schema):
    """Org-admin view of a single member row, with their live subscription inlined.

    ``subscription`` is the member's non-terminal subscription in this organization
    (``None`` when they have none) — at most one exists, per the
    ``one_active_subscription_per_user_org`` constraint. Banning, blacklisting or
    removing a member cancels it and stops Stripe billing, so the admin confirmation
    dialogs need it to warn truthfully. Mirrors :class:`MyMembershipSchema` on the
    member-facing side.

    Lives here rather than in ``schema/organization.py`` because that module is
    imported by this one; the nested subscription would be a circular import.
    """

    user: MemberUserSchema
    member_since: AwareDatetime = Field(alias="created_at")
    status: OrganizationMember.MembershipStatus
    tier: MembershipTierSchema | None = None
    subscription: SubscriptionSchema | None = None

    @staticmethod
    def resolve_subscription(obj: OrganizationMember) -> MembershipSubscription | None:
        """Return the member's non-terminal subscription in this organization, if any.

        Reads the ``_org_active_subs`` prefetch when present (set by the members-list
        queryset — avoids one query per row); falls back to a lookup for the
        single-object endpoints that return a member straight from the service layer.
        """
        prefetched: list[MembershipSubscription] | None = getattr(obj.user, "_org_active_subs", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return (
            MembershipSubscription.objects.filter(user_id=obj.user_id, organization_id=obj.organization_id)
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
            .select_related("user", "plan", "plan__tier")
            .first()
        )


class SubscribeRequestSchema(Schema):
    """Member-initiated subscribe payload."""

    plan_id: UUID


class SubscribeResponseSchema(Schema):
    """Response to a member-initiated subscribe.

    ``checkout_url`` is the hosted Stripe Checkout the frontend redirects the
    member to for payment — **or ``None`` for a FREE plan**, which involves no
    Stripe object at all. A null ``checkout_url`` means there is nothing left
    to do: ``subscription.status`` is already ``active`` (with a null
    ``current_period_end``, because FREE plans are LIFETIME and never renew)
    and the membership has been granted. The frontend must branch on it rather
    than redirecting unconditionally. Same contract as
    :class:`RevivalResponseSchema`.

    Nullable but **required**: the endpoint sets the key on both branches, so
    declaring a default would tell clients it may be absent — a distinction the
    frontend would have to handle for a case that never occurs.
    """

    subscription: MySubscriptionSchema
    checkout_url: str | None


class SubscriptionActivationPendingSchema(Schema):
    """Refusal body for a subscribe attempt whose checkout was already paid.

    The member completed Stripe Checkout and the activation webhooks are still
    in flight, so a second subscription must not be created. ``code`` is the
    machine-readable discriminator the frontend keys on to render a
    "confirming your subscription" state instead of an error — ``detail`` is
    translated and must never be matched on.

    The frontend branches on ``code == "subscription_activation_pending"``
    (member dialog and admin drawer both render it as "paid, activating — not
    cancelled"): renaming the code value is a breaking change; rewording or
    retranslating ``detail`` is always safe.
    """

    detail: str
    code: t.Literal["subscription_activation_pending"] = "subscription_activation_pending"


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

    # ``ge`` (not ``gt``): a zero-amount OFFLINE revival is a comped renewal an
    # organizer may legitimately want on the ledger, and both sibling money fields
    # in this module (``initial_payment_amount``, ``MembershipPaymentCreateSchema.amount``)
    # use ``ge=0``. Negative amounts reached the ledger layer unrejected before.
    amount: Decimal | None = Field(default=None, ge=Decimal("0"))
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
