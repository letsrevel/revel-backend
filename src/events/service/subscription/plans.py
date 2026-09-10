"""Membership subscription *plan* management — CRUD plus price migration.

Split out of :mod:`events.service.subscription.lifecycle` (which owns the
per-subscription state machine) so each module stays well inside the
1000-line cap: this one only ever operates on a
:class:`~events.models.MembershipSubscriptionPlan` and its Stripe Price,
never on a single subscriber's lifecycle.
"""

import typing as t
from decimal import Decimal

import structlog
from django.db import transaction
from django.db.models import ProtectedError
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
)
from events.service.subscription.notifications import _dispatch_price_migration, _format_money
from events.service.subscription.stripe import checkout as stripe_checkout
from events.service.subscription.stripe.base import ensure_stripe_price
from events.service.ticket_service import check_online_payment_prerequisites
from events.utils.subscription_plan_rules import validate_plan_shape
from notifications.enums import NotificationType

logger = structlog.get_logger(__name__)


def _maybe_sync_plan_to_stripe(plan: MembershipSubscriptionPlan) -> MembershipSubscriptionPlan:
    """Provision (or refresh) the Stripe Product+Price for an ONLINE plan.

    No-op for OFFLINE plans. Stripe failures bubble up as ``HttpError`` so the
    controller can return a clean ``502``; the DB transaction rolls back along
    with the plan write so we don't leave a half-provisioned row.

    Raises:
        StripeNotConnectedError: If the organization has no Stripe Connect account.
        BillingInfoRequiredError: If platform fees apply but billing info is incomplete.
    """
    if plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        return plan
    # ONLINE plans generate platform fees, so the org must be invoiceable — same
    # gate ONLINE ticket tiers pass through. Runs before ``ensure_stripe_price``
    # (whose own connectivity guard raises a generic 400) so callers get the
    # typed exceptions and their actionable messages.
    check_online_payment_prerequisites(plan.tier.organization)

    return ensure_stripe_price(plan)


@transaction.atomic
def create_plan(
    tier: MembershipTier,
    *,
    name: str,
    price: Decimal,
    currency: str,
    period_unit: str,
    period_count: int = 1,
    description: str = "",
    is_active: bool = True,
    payment_method: str = MembershipSubscriptionPlan.PaymentMethod.OFFLINE,
    sales_status: str = MembershipSubscriptionPlan.SalesStatus.OPEN,
    max_subscriptions: int | None = None,
) -> MembershipSubscriptionPlan:
    """Create a subscription plan for a membership tier.

    For ONLINE plans, also provisions the matching Stripe Product+Price on
    the organization's Connect account.

    A tier's eligibility gates (manual approval / membership questionnaire) and
    its plans coexist: ``/subscribe`` runs the full gate stack before opening
    Checkout, so gate config on a monetized tier is enforced, not inert.
    """
    plan = MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name=name,
        price=price,
        currency=currency,
        period_unit=period_unit,
        period_count=period_count,
        description=description,
        is_active=is_active,
        payment_method=payment_method,
        sales_status=sales_status,
        max_subscriptions=max_subscriptions,
    )
    return _maybe_sync_plan_to_stripe(plan)


@transaction.atomic
def update_plan(
    plan: MembershipSubscriptionPlan,
    **fields: t.Any,
) -> MembershipSubscriptionPlan:
    """Update a plan in-place.

    Callers pass only the fields to change; full_clean runs on save. When the
    plan is ONLINE and any pricing-shape field changes, the Stripe Price is
    archived and a fresh one created (Stripe Prices are immutable).

    Refuses currency changes when the plan has any non-terminal subscriptions
    — cross-currency migration is risky and out of roadmap; staff must archive
    and create a new plan instead.

    Also re-checks the (payment method, price, cadence) shape against the
    *merged* post-patch values: ``payment_method`` is not patchable, so a FREE
    plan can never acquire a price and an ONLINE plan can never lose one or
    become LIFETIME.
    """
    if not fields:
        return plan

    shape_error = validate_plan_shape(
        payment_method=plan.payment_method,
        price=fields.get("price", plan.price),
        period_unit=fields.get("period_unit", plan.period_unit),
    )
    if shape_error:
        raise HttpError(400, shape_error)

    new_currency = fields.get("currency")
    if new_currency is not None and new_currency.upper() != plan.currency.upper():
        has_active_subs = (
            MembershipSubscription.objects.filter(plan=plan)
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
            .exists()
        )
        if has_active_subs:
            msg = _("Cannot change currency when active subscriptions exist. Archive and create a new plan instead.")
            raise HttpError(400, str(msg))

    for field, value in fields.items():
        setattr(plan, field, value)
    plan.save(update_fields=[*fields.keys(), "updated_at"])
    return _maybe_sync_plan_to_stripe(plan)


@transaction.atomic
def archive_plan(plan: MembershipSubscriptionPlan) -> MembershipSubscriptionPlan:
    """Soft-disable a plan by flipping ``is_active``.

    For ONLINE plans, also archives the Stripe Price so it can't be used for
    new subscriptions. Existing subscribers keep paying their old Price.
    """
    if plan.is_active:
        plan.is_active = False
        plan.save(update_fields=["is_active", "updated_at"])
    if plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        stripe_checkout.archive_stripe_price(plan)
    return plan


@transaction.atomic
def delete_plan(plan: MembershipSubscriptionPlan) -> None:
    """Hard-delete a plan.

    Raises 400 if any subscription references it — staff should archive
    instead.
    """
    if plan.subscriptions.exists():
        raise HttpError(400, str(_("Cannot delete a plan with existing subscriptions. Archive it instead.")))
    try:
        plan.delete()
    except ProtectedError as exc:
        # Concurrent ``create_subscription`` slipped in between our existence
        # check and the delete: PROTECT raises ProtectedError which would
        # otherwise bubble up as a 500.
        raise HttpError(400, str(_("Cannot delete a plan with existing subscriptions. Archive it instead."))) from exc


class MigrationError(t.TypedDict):
    """Per-subscription error record produced by :func:`migrate_plan_subscribers`."""

    sub_id: str
    reason: str


class MigrationResult(t.TypedDict):
    """Aggregate result of a :func:`migrate_plan_subscribers` call."""

    migrated: int
    skipped: int
    skipped_schedule_managed: int
    failed: int
    errors: list[MigrationError]


def migrate_plan_subscribers(
    plan: MembershipSubscriptionPlan,
    *,
    initiated_by: RevelUser,
) -> MigrationResult:
    """Force-migrate non-terminal subscriptions on ``plan`` to its current price.

    For ONLINE subs: calls stripe_checkout.update_subscription_price,
    which issues stripe.Subscription.modify(proration_behavior='none'). The new
    price takes effect at the next renewal.

    For OFFLINE subs: no Stripe call; just notifies that next renewal will be
    at the new amount.

    Per-subscription errors are captured in result["errors"]; successful
    migrations are not rolled back. Re-running the endpoint after a partial
    failure is safe: ONLINE subs already on the current Stripe price are
    counted as ``skipped``, and OFFLINE subs whose price-change notice for
    this exact change was already sent are ``skipped`` too — the migration
    writes nothing to an OFFLINE row, so the notification ledger is the
    idempotency anchor that keeps a re-run (staff double-click, acks_late
    redelivery) from re-spamming subscribers.

    Schedule-managed ONLINE subs (a pending downgrade) are counted under
    ``skipped_schedule_managed`` and never touched — same carve-out, and same
    reasons, as :func:`stripe_checkout.resync_subscription_application_fees`.
    Re-run the migration once their schedule releases.
    """
    result: MigrationResult = {
        "migrated": 0,
        "skipped": 0,
        "skipped_schedule_managed": 0,
        "failed": 0,
        "errors": [],
    }
    qs = (
        MembershipSubscription.objects.filter(plan=plan)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .select_related("plan", "organization", "user")
    )
    new_price = plan.price

    # Single-query lookup for the subscriber's last SUCCEEDED payment amount,
    # to avoid N+1 inside the migration loop. Postgres DISTINCT ON picks the
    # most-recent row per subscription according to the ORDER BY. Proration
    # invoices from a mid-cycle upgrade are a partial-period delta, never the
    # subscriber's old per-period price, so they cannot anchor the notice.
    old_price_by_sub: dict[t.Any, Decimal] = dict(
        MembershipPayment.objects.filter(
            subscription__in=qs,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
        )
        .exclude(raw_response__contains={"billing_reason": "subscription_update"})
        .order_by("subscription_id", "-created_at")
        .distinct("subscription_id")
        .values_list("subscription_id", "amount")
    )

    for sub in qs:
        try:
            if sub.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE:
                if sub.stripe_schedule_id:
                    # Stripe rejects a plain ``Subscription.modify`` while a schedule
                    # is attached, and releasing the schedule would silently drop the
                    # pending plan change. Without this the modify 502s straight into
                    # ``failed``, where a staff-triggered batch (202 + logs only)
                    # reads as a Stripe outage rather than the deliberate carve-out
                    # it is.
                    result["skipped_schedule_managed"] += 1
                    logger.warning(
                        "migrate_plan_subscribers_skipped_schedule_managed",
                        plan_id=str(plan.pk),
                        subscription_id=str(sub.pk),
                        schedule_id=sub.stripe_schedule_id,
                    )
                    continue
                changed = stripe_checkout.update_subscription_price(sub)
                if not changed:
                    result["skipped"] += 1
                    continue
            # OFFLINE: no Stripe call. We still dispatch the notification so
            # the subscriber knows next renewal will be at the new amount.

            old_price = old_price_by_sub.get(sub.id)
            if old_price is None or old_price == new_price:
                # Skip the price-migration notification when there's no prior
                # successful payment to anchor against (would render X→X), or
                # when the subscriber already paid the new price.
                result["migrated"] += 1
                continue
            if sub.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.OFFLINE and (
                _price_migration_already_notified(sub, new_price)
            ):
                # OFFLINE re-run safety: nothing on the row records the
                # migration (the last-paid-price anchor stays stale until the
                # next renewal), so dedupe on the already-sent notice.
                result["skipped"] += 1
                continue
            _dispatch_price_migration(sub, old_price=old_price, new_price=new_price)
            result["migrated"] += 1
        except Exception as exc:  # noqa: BLE001 — caught for per-sub reporting
            result["failed"] += 1
            result["errors"].append({"sub_id": str(sub.id), "reason": str(exc)})
            logger.error(
                "migrate_plan_subscribers_failed_one",
                plan_id=str(plan.pk),
                subscription_id=str(sub.pk),
                error=str(exc),
            )

    logger.info(
        "migrate_plan_subscribers_done",
        plan_id=str(plan.pk),
        initiated_by=str(initiated_by.id),
        **result,
    )
    return result


def _price_migration_already_notified(subscription: MembershipSubscription, new_price: t.Any) -> bool:
    """True when this subscriber already received the notice for this exact price change."""
    from notifications.models import Notification  # lazy: keep app import edges thin

    plan = subscription.plan
    return Notification.objects.filter(
        user=subscription.user,
        notification_type=NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        context__organization_slug=subscription.organization.slug,
        context__plan_name=plan.name,
        context__new_amount=_format_money(new_price, plan.currency),
    ).exists()
