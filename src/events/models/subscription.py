"""Membership subscription models.

Phase 1 introduced staff-managed (OFFLINE) subscriptions. Phase 2 adds
Stripe-backed ONLINE subscriptions via additive fields and a per-(user, org)
:class:`CustomerProfile`.
"""

import typing as t

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.fields import MarkdownField
from common.models import TimeStampedModel

from .organization import MembershipTier, Organization

# Single source of truth for the subscription statuses that are terminal
# (no further transitions possible). Lives at module scope so it can be
# referenced both from ``MembershipSubscription.Meta.constraints`` and from
# runtime callers via ``MembershipSubscription.TERMINAL_STATUSES``.
_TERMINAL_STATUS_VALUES: tuple[str, ...] = ("cancelled", "expired")


class MembershipSubscriptionPlan(TimeStampedModel):
    """A paid plan attached to a :class:`MembershipTier`.

    Multiple plans can target the same tier (e.g. "Monthly" + "Annual"). The
    billing cadence is rolling only: ``period_count`` units of ``period_unit``
    from each renewal.
    """

    class PeriodUnit(models.TextChoices):
        MONTH = "month", _("Month")
        YEAR = "year", _("Year")

    class PaymentMethod(models.TextChoices):
        ONLINE = "online", _("Online (Stripe)")
        OFFLINE = "offline", _("Offline (staff-managed)")

    tier = models.ForeignKey(
        MembershipTier,
        on_delete=models.CASCADE,
        related_name="subscription_plans",
    )
    name = models.CharField(max_length=255, help_text=_('e.g. "Monthly", "Annual"'))
    description = MarkdownField(blank=True, default="")
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    currency = models.CharField(max_length=3, help_text=_("ISO 4217 currency code"))
    period_unit = models.CharField(
        max_length=10,
        choices=PeriodUnit.choices,
        default=PeriodUnit.MONTH,
    )
    period_count = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    is_active = models.BooleanField(default=True, db_index=True)
    payment_method = models.CharField(
        max_length=10,
        choices=PaymentMethod.choices,
        default=PaymentMethod.OFFLINE,
        help_text=_("ONLINE plans are billed via Stripe; OFFLINE plans are tracked manually by staff."),
    )
    stripe_product_id = models.CharField(max_length=255, blank=True, default="")
    stripe_price_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["tier__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["tier", "name"], name="unique_plan_name_per_tier"),
        ]

    def __str__(self) -> str:
        return f"{self.tier} - {self.name}"

    @property
    def organization_id(self) -> t.Any:
        """Convenience proxy for permission / scoping helpers."""
        return self.tier.organization_id


class MembershipSubscription(TimeStampedModel):
    """A user's subscription to a :class:`MembershipSubscriptionPlan`.

    ``organization`` is denormalized for query/permission simplicity and kept
    consistent with the plan's tier via ``clean()``.
    """

    class SubscriptionStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACTIVE = "active", _("Active")
        PAUSED = "paused", _("Paused")
        PAST_DUE = "past_due", _("Past Due")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")

    TERMINAL_STATUSES: t.ClassVar[frozenset[str]] = frozenset(_TERMINAL_STATUS_VALUES)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="membership_subscriptions",
    )
    plan = models.ForeignKey(
        MembershipSubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="subscriptions",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="membership_subscriptions",
    )
    status = models.CharField(
        max_length=16,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.PENDING,
        db_index=True,
    )
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True, db_index=True)
    cancel_at_period_end = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, null=True, blank=True, unique=True)
    pending_plan = models.ForeignKey(
        MembershipSubscriptionPlan,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pending_subscriptions",
        help_text=_("Plan that will replace ``plan`` at the next renewal (downgrade flow)."),
    )
    stripe_schedule_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                condition=~models.Q(status__in=list(_TERMINAL_STATUS_VALUES)),
                name="one_active_subscription_per_user_org",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} @ {self.organization_id} ({self.status})"

    def clean(self) -> None:
        """Enforce subscription/plan-tier organization integrity."""
        super().clean()
        if self.plan_id and self.organization_id and self.plan.tier.organization_id != self.organization_id:
            raise DjangoValidationError(
                {"organization": _("Subscription organization must match the plan's tier organization.")}
            )
        if (
            self.pending_plan_id
            and self.pending_plan
            and self.pending_plan.tier.organization_id != self.organization_id
        ):
            raise DjangoValidationError(
                {"pending_plan": _("Pending plan must belong to the same organization as the subscription.")}
            )

    @property
    def is_terminal(self) -> bool:
        """True if the subscription is in a terminal state."""
        return self.status in self.TERMINAL_STATUSES


class MembershipPayment(TimeStampedModel):
    """A single payment recorded against a :class:`MembershipSubscription`.

    In Phase 1 these are recorded manually by staff. ``raw_response`` is
    reserved for Phase 2 Stripe payloads (invoice / payment_intent JSON).
    """

    class PaymentStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")
        REFUNDED = "refunded", _("Refunded")

    subscription = models.ForeignKey(
        MembershipSubscription,
        on_delete=models.CASCADE,
        related_name="payments",
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=16,
        choices=PaymentStatus.choices,
        default=PaymentStatus.SUCCEEDED,
        db_index=True,
    )
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_membership_payments",
    )
    notes = models.TextField(blank=True, default="")
    raw_response = models.JSONField(default=dict, blank=True)
    stripe_invoice_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["subscription", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Payment {self.id} for sub {self.subscription_id} ({self.status})"


class CustomerProfile(TimeStampedModel):
    """Per-(user, organization) Stripe Customer reference.

    Stripe Connect uses one Customer namespace per connected account, so a
    single platform-wide Customer doesn't work — each org's Stripe account
    needs its own Customer for the user. This model is the join.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="customer_profiles",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="customer_profiles",
    )
    stripe_customer_id = models.CharField(max_length=255, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "organization"], name="unique_customer_per_user_org"),
            models.UniqueConstraint(
                fields=["organization", "stripe_customer_id"],
                name="unique_stripe_customer_per_org",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} @ {self.organization_id} ({self.stripe_customer_id})"
