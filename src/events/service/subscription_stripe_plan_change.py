"""Plan-change helpers for ONLINE membership subscriptions (Phase 3).

Extracted from ``subscription_stripe_service`` to keep that module under the
1000-line file-length limit. All functions here deal exclusively with
switching an ONLINE subscription from one :class:`MembershipSubscriptionPlan`
to another — either as an immediate prorated upgrade or as a scheduled
downgrade via a :class:`stripe.SubscriptionSchedule`.
"""

import typing as t
from decimal import Decimal

import stripe
import structlog
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    Organization,
)
from events.service.subscription_stripe_base import ensure_stripe_price
from events.service.subscription_stripe_payloads import _is_subscription_gone, _stripe_account_kwargs

logger = structlog.get_logger(__name__)


# ---- Internal helpers --------------------------------------------------------


# LIFETIME is deliberately absent: this module is ONLINE-only (every entry
# point guards on ``payment_method == ONLINE``), ``change_plan`` refuses
# cross-payment-method switches, and an ONLINE plan can never be LIFETIME
# (rejected at create and patch by ``validate_plan_shape``). A KeyError here
# would therefore mean one of those three invariants broke — which is exactly
# the loud failure we want, rather than a silently wrong proration.
_PERIOD_UNIT_MONTHS: dict[str, Decimal] = {
    MembershipSubscriptionPlan.PeriodUnit.MONTH.value: Decimal("1"),
    MembershipSubscriptionPlan.PeriodUnit.YEAR.value: Decimal("12"),
}


def _monthly_equivalent_price(plan: MembershipSubscriptionPlan) -> Decimal:
    """Return ``plan.price`` normalized to a per-month figure.

    A cross-cadence change (e.g. Monthly→Annual) must compare like-for-like
    or the raw ``price`` comparison wrongly classifies "cheaper per month
    but higher headline" as an upgrade and fires immediate proration.
    """
    months = _PERIOD_UNIT_MONTHS[plan.period_unit] * Decimal(plan.period_count)
    return plan.price / months


def _classify_plan_change(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> t.Literal["upgrade", "downgrade"]:
    """Return ``"upgrade"`` when new_plan costs more per month; ``"downgrade"`` otherwise."""
    return (
        "upgrade" if _monthly_equivalent_price(new_plan) > _monthly_equivalent_price(subscription.plan) else "downgrade"
    )


def release_online_schedule(subscription: MembershipSubscription) -> None:
    """Release any Stripe SubscriptionSchedule bound to ``subscription``, clearing local state.

    A scheduled downgrade sets ``stripe_schedule_id`` and makes the
    subscription *schedule-managed* on Stripe. In that state Stripe rejects a
    plain ``cancel_at_period_end`` / ``pause_collection`` modify with an opaque
    error, which surfaces to the member as a 502. Releasing the schedule hands
    control back to the underlying subscription so the cancel/pause can proceed
    at its current price, and the pending downgrade is dropped (a member who
    cancels or pauses has abandoned that downgrade).

    No-op when the row has no schedule. Tolerates a schedule Stripe has already
    released or completed (``InvalidRequestError`` → log and proceed). A hard
    Stripe failure propagates as a 502 so we don't then attempt a modify that
    would also fail.
    """
    if not subscription.stripe_schedule_id:
        return
    kwargs = _stripe_account_kwargs(subscription.organization)
    try:
        # The stub types the first arg as a SubscriptionSchedule; the runtime API
        # accepts the schedule id string (as elsewhere in this module).
        stripe.SubscriptionSchedule.release(subscription.stripe_schedule_id, **kwargs)  # type: ignore[arg-type]
    except stripe.error.InvalidRequestError as exc:
        # Already released/completed on Stripe — nothing to undo; proceed to
        # clear local state so the subscription is no longer schedule-managed.
        logger.info(
            "subscription_schedule_release_already_done",
            subscription_id=str(subscription.pk),
            schedule_id=subscription.stripe_schedule_id,
            error=str(exc),
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_schedule_release_failed",
            subscription_id=str(subscription.pk),
            schedule_id=subscription.stripe_schedule_id,
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    update_fields = ["stripe_schedule_id", "updated_at"]
    subscription.stripe_schedule_id = ""
    if subscription.pending_plan_id:
        subscription.pending_plan = None
        update_fields.append("pending_plan")
    subscription.save(update_fields=update_fields)


def resolve_refused_cancel(
    subscription: MembershipSubscription,
    exc: stripe.error.InvalidRequestError,
    *,
    reason: str,
) -> bool:
    """Decide what a refused best-effort ``Subscription.cancel`` actually means.

    Stripe raises :class:`InvalidRequestError` both when the subscription is
    already gone (cancelled/deleted — the caller's desired end state) and when it
    is *schedule-managed*, i.e. a pending downgrade is still attached. Reading
    the latter as success left Stripe billing a member who had already lost
    access locally, so the two are told apart here: a schedule is released (a row
    being terminalized has no use for its pending downgrade) and the cancel
    retried **once**, which is why this resolver lives next to
    :func:`release_online_schedule`.

    Best-effort like its caller
    (:func:`subscription_stripe_service.cancel_stripe_subscription_best_effort`):
    every failure — including the 502 :func:`release_online_schedule` raises on a
    hard Stripe error — is logged and reported as ``False`` so callers keep
    treating the Stripe subscription as possibly still live and billing.

    Args:
        subscription: The locally-terminalized row whose Stripe sub must close.
        exc: The refusal ``Subscription.cancel`` raised.
        reason: The caller's terminalization reason, for the log line.

    Returns:
        True when the Stripe subscription is known to be closed.
    """
    if _is_subscription_gone(exc):
        logger.info(
            "subscription_stripe_cancel_on_terminalize_already_done",
            subscription_id=str(subscription.pk),
            stripe_subscription_id=subscription.stripe_subscription_id,
            reason=reason,
        )
        return True

    if not subscription.stripe_schedule_id and "schedule" not in str(exc).lower():
        logger.error(
            "subscription_stripe_cancel_on_terminalize_failed",
            subscription_id=str(subscription.pk),
            stripe_subscription_id=subscription.stripe_subscription_id,
            reason=reason,
            error=str(exc),
        )
        return False

    try:
        release_online_schedule(subscription)
        stripe.Subscription.cancel(  # type: ignore[attr-defined]
            subscription.stripe_subscription_id,
            **_stripe_account_kwargs(subscription.organization),
        )
    except stripe.error.InvalidRequestError as retry_exc:
        if not _is_subscription_gone(retry_exc):
            logger.error(
                "subscription_stripe_cancel_on_terminalize_failed",
                subscription_id=str(subscription.pk),
                stripe_subscription_id=subscription.stripe_subscription_id,
                reason=reason,
                error=str(retry_exc),
            )
            return False
    except stripe.error.StripeError, HttpError:
        logger.exception(
            "subscription_stripe_cancel_on_terminalize_failed",
            subscription_id=str(subscription.pk),
            stripe_subscription_id=subscription.stripe_subscription_id,
            reason=reason,
        )
        return False

    logger.info(
        "subscription_stripe_cancelled_on_terminalize",
        subscription_id=str(subscription.pk),
        stripe_subscription_id=subscription.stripe_subscription_id,
        reason=reason,
        after_schedule_release=True,
    )
    return True


def _retrieve_subscription_item_id(stripe_subscription_id: str, org: Organization) -> str:
    """Return the first Subscription Item id from a live Stripe Subscription."""
    try:
        stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id, **_stripe_account_kwargs(org))
    except stripe.error.StripeError as exc:
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc
    items = (stripe_sub.get("items") or {}).get("data") or []
    if not items:
        raise HttpError(502, str(_("Stripe subscription has no items to update.")))
    return t.cast(str, items[0]["id"])


def _upgrade_online_subscription(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> MembershipSubscription:
    """Apply an immediate, prorated price swap on Stripe.

    ``always_invoice`` — not ``create_prorations`` — because the tier is granted
    synchronously below (the ``MembershipSubscription`` save re-points
    ``OrganizationMember.tier``). ``create_prorations`` only queues line items
    for the *next* invoice, so the member would hold the pricier tier for a full
    cycle without being charged, and an immediate cancel — which Stripe defaults
    to ``invoice_now=False, prorate=False`` — would discard the delta outright.
    Invoicing on the spot keeps the grant and the charge together.

    If that invoice's payment fails the subscription moves to ``past_due`` and
    the existing dunning flow takes over; either way the price swap stands.
    """
    org = subscription.organization
    kwargs = _stripe_account_kwargs(org)
    stripe_sub_id = t.cast(str, subscription.stripe_subscription_id)
    item_id = _retrieve_subscription_item_id(stripe_sub_id, org)
    try:
        stripe.Subscription.modify(
            stripe_sub_id,
            items=[{"id": item_id, "price": new_plan.stripe_price_id}],
            proration_behavior="always_invoice",
            payment_behavior="allow_incomplete",
            metadata={"revel_plan_id": str(new_plan.pk)},
            **kwargs,
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_upgrade_failed",
            subscription_id=str(subscription.pk),
            new_plan_id=str(new_plan.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    # Reflect the price swap locally right away so the API response sees the
    # new plan without waiting for the ``customer.subscription.updated`` webhook
    # round-trip. The webhook re-applies the same state idempotently.
    subscription.plan = new_plan
    subscription.pending_plan = None
    subscription.save(update_fields=["plan", "pending_plan", "updated_at"])
    return subscription


def _downgrade_online_subscription(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> MembershipSubscription:
    """Schedule a price swap at the next renewal via a Stripe Subscription Schedule.

    Two-phase schedule: phase 1 keeps the current price for the rest of the
    current period; phase 2 starts the new price at the period boundary and
    lasts one billing period of the new plan (``duration``).
    ``end_behavior='release'`` lets the subscription fall back to a normal
    rolling renewal at the new price once that second phase completes.

    The hand-built phases carry no ``application_fee_percent`` on purpose:
    ``from_subscription`` copies the subscription's fee into the schedule's
    ``default_settings``, the phases rewrite below leaves ``default_settings``
    untouched, so the phases inherit it and the subscription keeps its own fee
    for after the release. Verified empirically against the live test-mode API
    at pinned version 2026-03-25.dahlia on 2026-07-29 (#821); canary test:
    ``events/tests/test_service/test_stripe_schedule_fee_integration.py``.
    """
    org = subscription.organization
    kwargs = _stripe_account_kwargs(org)
    try:
        schedule = stripe.SubscriptionSchedule.create(
            from_subscription=subscription.stripe_subscription_id,
            **kwargs,
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_downgrade_failed",
            subscription_id=str(subscription.pk),
            new_plan_id=str(new_plan.pk),
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    try:
        current_phase = (schedule.get("phases") or [None])[0]
        if not current_phase:
            raise HttpError(502, str(_("Stripe did not return a schedule phase to extend.")))
        existing_price = ((current_phase.get("items") or [{}])[0].get("price")) or subscription.plan.stripe_price_id
        new_phases: list[dict[str, t.Any]] = [
            {
                "items": [{"price": existing_price, "quantity": 1}],
                "start_date": current_phase.get("start_date"),
                "end_date": current_phase.get("end_date"),
                "proration_behavior": "none",
            },
            {
                "items": [{"price": new_plan.stripe_price_id, "quantity": 1}],
                # ``iterations`` was removed by the pinned Stripe API version
                # (>= 2025-07-30.basil rejects it with parameter_unknown); a
                # ``duration`` of one new-plan period is the replacement. It is
                # mutually exclusive with ``end_date``, which phase 2 has none of.
                "duration": {"interval": new_plan.period_unit, "interval_count": new_plan.period_count},
                "proration_behavior": "none",
            },
        ]
        stripe.SubscriptionSchedule.modify(
            schedule.id,
            end_behavior="release",
            phases=new_phases,
            metadata={"revel_subscription_id": str(subscription.pk), "revel_new_plan_id": str(new_plan.pk)},
            **kwargs,
        )
    except (stripe.error.StripeError, HttpError) as exc:
        # Best-effort release of the orphaned schedule so it doesn't keep applying.
        try:
            stripe.SubscriptionSchedule.release(schedule.id, **kwargs)
        except stripe.error.StripeError as release_exc:
            logger.error(
                "subscription_stripe_downgrade_schedule_release_failed",
                subscription_id=str(subscription.pk),
                new_plan_id=str(new_plan.pk),
                schedule_id=str(schedule.id),
                error=str(release_exc),
            )
        logger.error(
            "subscription_stripe_downgrade_failed",
            subscription_id=str(subscription.pk),
            new_plan_id=str(new_plan.pk),
            error=str(exc),
        )
        if isinstance(exc, HttpError):
            raise
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc

    subscription.pending_plan = new_plan
    subscription.stripe_schedule_id = t.cast(str, schedule.id)
    subscription.save(update_fields=["pending_plan", "stripe_schedule_id", "updated_at"])
    return subscription


def _validate_change_plan_state(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> None:
    """Pre-flight checks shared by upgrade and downgrade routing.

    PAST_DUE is refused alongside the other non-steady states: an upgrade
    invoices only the proration delta (``always_invoice``), and when *that*
    invoice settles ``_apply_invoice_outcome`` revives the row to ACTIVE
    (PAST_DUE is in its revivable set) while the lapsed renewal invoice stays
    unpaid and the period anchor stays in the past — a false ACTIVE that also
    grants the pricier tier and clears the dunning warning. Settling the
    outstanding invoice (Customer Portal) is the documented remediation.
    """
    if subscription.is_terminal:
        raise HttpError(400, str(_("Cannot change the plan on a terminated subscription.")))
    if subscription.status == MembershipSubscription.SubscriptionStatus.PAUSED:
        raise HttpError(400, str(_("Resume the subscription before changing its plan.")))
    if subscription.status == MembershipSubscription.SubscriptionStatus.PAST_DUE:
        raise HttpError(400, str(_("Settle the outstanding payment before changing plans.")))
    if subscription.cancel_at_period_end:
        raise HttpError(400, str(_("This subscription is scheduled to cancel; cannot change plan.")))
    if subscription.pending_plan_id:
        raise HttpError(400, str(_("A plan change is already pending on this subscription.")))
    if subscription.plan_id == new_plan.pk:
        raise HttpError(400, str(_("This subscription is already on that plan.")))


def _ensure_new_plan_has_stripe_price(new_plan: MembershipSubscriptionPlan) -> None:
    """Lazy-provision the Stripe Price for ``new_plan`` if it's missing.

    Plans created before Phase 2 (or whose initial sync failed) might be
    missing ``stripe_price_id``. Provisioning here lets a member still switch
    to them without a manual fix-up step.
    """
    if new_plan.stripe_price_id:
        return
    ensure_stripe_price(new_plan)
    new_plan.refresh_from_db()
    if not new_plan.stripe_price_id:
        raise HttpError(500, str(_("Could not prepare the plan for checkout.")))


def change_online_plan(
    subscription: MembershipSubscription,
    new_plan: MembershipSubscriptionPlan,
) -> MembershipSubscription:
    """Switch the plan on an ONLINE subscription.

    Routes to :func:`_upgrade_online_subscription` or
    :func:`_downgrade_online_subscription` based on the monthly-equivalent
    price delta. Currency parity is enforced upstream by
    ``subscription_service._validate_change_plan_target``.

    Validation runs under a ``select_for_update`` lock taken in an inner
    ``transaction.atomic()``. NOTE: under production ATOMIC_REQUESTS the
    inner block exit releases only a savepoint — the row lock is actually
    held until the request transaction commits, i.e. across the Stripe
    round-trips below. That is deliberate for now: the lock is what
    serializes echo-webhooks against this mutation (the webhook's own
    ``select_for_update`` blocks until we commit, then sees the updated
    local flags and suppresses duplicate dispatch). Contention is per-user
    per-subscription, so the blast radius is a single member's requests.
    Concurrent attempts are additionally serialized because Stripe rejects
    a second ``SubscriptionSchedule.create`` against the same
    ``from_subscription`` once one already exists.
    """
    if subscription.plan.payment_method != MembershipSubscriptionPlan.PaymentMethod.ONLINE:
        raise HttpError(400, str(_("This subscription is not managed by Stripe.")))
    if not subscription.stripe_subscription_id:
        raise HttpError(400, str(_("This subscription has no linked Stripe record yet.")))

    _ensure_new_plan_has_stripe_price(new_plan)

    with transaction.atomic():
        subscription = (
            MembershipSubscription.objects.select_for_update(of=("self",))
            .select_related("plan", "plan__tier", "organization", "user")
            .get(pk=subscription.pk)
        )
        _validate_change_plan_state(subscription, new_plan)
        classification = _classify_plan_change(subscription, new_plan)

    # Under ATOMIC_REQUESTS the row lock is still held here (see docstring).
    if classification == "upgrade":
        return _upgrade_online_subscription(subscription, new_plan)
    return _downgrade_online_subscription(subscription, new_plan)
