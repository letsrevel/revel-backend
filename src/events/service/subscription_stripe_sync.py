"""Webhook-side mirror for Stripe membership subscriptions.

Split out of :mod:`events.service.subscription_stripe_service` (file-length
cap): everything here is driven by inbound Stripe payloads — webhook events
(``customer.subscription.*``, ``invoice.*``) and the nightly reconcile task —
never by member/staff API calls. The local state machine's terminal statuses
are authoritative and are never overwritten by Stripe payloads.
"""

import typing as t
from datetime import datetime
from decimal import Decimal

import stripe
import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from common.models import SiteSettings
from common.service.vat_utils import b2b_fee_vat_from_gross
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    OrganizationMember,
)
from events.service import stripe_incidents
from events.service.subscription_stripe_dispatch import (
    _dispatch_invoice_notifications,
    _dispatch_sync_notifications,
)
from events.service.subscription_stripe_payloads import (
    _epoch_to_dt,
    _invoice_payment_intent_id,
    _invoice_subscription_id,
    _subscription_period_epochs,
)
from events.utils.currency import from_stripe_amount

logger = structlog.get_logger(__name__)

# Pin credentials + API version at import time (mirrors subscription_stripe_service).
stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION


# ---- Webhook helpers --------------------------------------------------------


def _ensure_active_member(subscription: MembershipSubscription) -> None:
    """Make sure an :class:`OrganizationMember` exists for an ONLINE subscriber.

    Phase 1's signal-driven sync intentionally never *creates* members; that
    responsibility belongs to :func:`subscription_service.create_subscription`
    for the OFFLINE flow. For ONLINE plans, the equivalent moment is the
    first successful invoice / Stripe ``active`` status — both of which land
    in this module's webhook helpers.

    An existing row (BANNED included) is left untouched. When no row exists we
    normally create one — a plain removal now cancels the subscription up front,
    so a later ``invoice.paid`` is a genuine re-subscribe. The one exception is a
    *hard-blacklisted* user: minting an ACTIVE member would silently un-ban them,
    so we record the payment (money already moved) and raise an incident for the
    manual refund/cancel instead. This is a rare race (payment in flight while
    staff ban) now that ban/removal cancels the subscription.
    """
    existing = OrganizationMember.objects.filter(
        organization=subscription.organization,
        user=subscription.user,
    ).first()
    if existing is not None:
        return

    from events.service.blacklist_service import check_user_hard_blacklisted  # lazy: avoid cycle

    if check_user_hard_blacklisted(subscription.user, subscription.organization):
        stripe_incidents.record_subscription_paid_while_blacklisted(
            subscription_id=str(subscription.pk),
            organization_id=str(subscription.organization_id),
            user_id=str(subscription.user_id),
            user_email=subscription.user.email,
            stripe_subscription_id=subscription.stripe_subscription_id or "",
        )
        return

    OrganizationMember.objects.get_or_create(
        organization=subscription.organization,
        user=subscription.user,
        defaults={
            "tier": subscription.plan.tier,
            "status": OrganizationMember.MembershipStatus.ACTIVE,
        },
    )


_STRIPE_STATUS_MAP: dict[str, str] = {
    "incomplete": MembershipSubscription.SubscriptionStatus.PENDING.value,
    "incomplete_expired": MembershipSubscription.SubscriptionStatus.EXPIRED.value,
    "trialing": MembershipSubscription.SubscriptionStatus.ACTIVE.value,
    "active": MembershipSubscription.SubscriptionStatus.ACTIVE.value,
    "past_due": MembershipSubscription.SubscriptionStatus.PAST_DUE.value,
    "unpaid": MembershipSubscription.SubscriptionStatus.PAST_DUE.value,
    "canceled": MembershipSubscription.SubscriptionStatus.CANCELLED.value,
    "paused": MembershipSubscription.SubscriptionStatus.PAUSED.value,
}


def map_stripe_status(stripe_status: str) -> str | None:
    """Translate a Stripe ``Subscription.status`` to our local enum value."""
    return _STRIPE_STATUS_MAP.get(stripe_status)


def _resolve_target_status(stripe_subscription: dict[str, t.Any]) -> str | None:
    """Translate the Stripe payload to a local status, honoring ``pause_collection``.

    Stripe surfaces an active pause via the ``pause_collection`` object, not
    via the top-level ``status`` field. When set, we treat the subscription
    as PAUSED locally — *unless* the mapped Stripe status is terminal
    (CANCELLED/EXPIRED). Terminal wins so a deletion event that still carries
    a stale ``pause_collection`` doesn't un-terminalize the local row and
    re-arm the one-active-subscription-per-(user, org) unique index.
    """
    mapped = map_stripe_status(t.cast(str, stripe_subscription.get("status", "")))
    if mapped in MembershipSubscription.TERMINAL_STATUSES:
        return mapped
    if stripe_subscription.get("pause_collection"):
        return MembershipSubscription.SubscriptionStatus.PAUSED.value
    return mapped


def _apply_period_dates(
    subscription: MembershipSubscription,
    stripe_subscription: dict[str, t.Any],
) -> list[str]:
    """Mirror Stripe's ``current_period_*`` epochs onto the local row in place."""
    changed: list[str] = []
    start_epoch, end_epoch = _subscription_period_epochs(stripe_subscription)
    new_start = _epoch_to_dt(start_epoch)
    if new_start and subscription.current_period_start != new_start:
        subscription.current_period_start = new_start
        changed.append("current_period_start")
    new_end = _epoch_to_dt(end_epoch)
    if new_end and subscription.current_period_end != new_end:
        subscription.current_period_end = new_end
        changed.append("current_period_end")
    return changed


def _apply_stripe_price_swap(
    subscription: MembershipSubscription,
    stripe_subscription: dict[str, t.Any],
) -> list[str]:
    """Detect a Stripe price swap and re-point ``subscription.plan`` if needed.

    Returns the list of field names that were mutated on ``subscription`` so
    the caller can extend its own ``update_fields`` list. Mutates the
    instance in place but does not save. Terminal rows are frozen — late
    webhook events for a cancelled/expired subscription must not rewrite
    the historical plan FK.

    ``stripe_schedule_id`` is deliberately left alone. A scheduled downgrade's
    swap lands at the phase-1→2 rollover, but Stripe keeps managing the
    subscription until phase 2 *ends* — a full billing period later. Clearing
    the id here would make ``release_online_schedule`` a no-op for that whole
    period, and the member's cancel would then be rejected by Stripe (a
    schedule-managed subscription refuses ``cancel_at_period_end``) while we
    happily recorded it locally. ``handle_subscription_schedule_released``
    clears the id when Stripe actually releases the schedule.
    """
    if subscription.is_terminal:
        return []
    items = (stripe_subscription.get("items") or {}).get("data") or []
    active_price_id = (items[0].get("price", {}).get("id") if items else None) or None
    if not active_price_id or active_price_id == subscription.plan.stripe_price_id:
        return []
    new_plan = MembershipSubscriptionPlan.objects.filter(
        stripe_price_id=active_price_id,
        tier__organization=subscription.organization,
    ).first()
    if not new_plan or new_plan.pk == subscription.plan_id:
        return []
    changed = ["plan"]
    subscription.plan = new_plan
    if subscription.pending_plan_id == new_plan.pk:
        subscription.pending_plan = None
        changed.append("pending_plan")
    return changed


def _apply_status_transition(
    subscription: MembershipSubscription,
    stripe_subscription: dict[str, t.Any],
    *,
    prior_cap: bool,
) -> list[str]:
    """Resolve and apply the payload's status onto the row; return mutated field names.

    Mutates the instance in place but does not save. Handles the lifecycle
    timestamps that ride along with a status change: ``cancelled_at`` /
    ``expired_at`` stamping on the way into a terminal state, and the
    ``expired_at`` clear on the way back to ACTIVE.
    """
    target_status = _resolve_target_status(stripe_subscription)
    if (
        target_status == MembershipSubscription.SubscriptionStatus.CANCELLED.value
        and subscription.status == MembershipSubscription.SubscriptionStatus.PAST_DUE.value
        and not prior_cap
    ):
        # Stripe's dunning gave up (``customer.subscription.deleted`` carries
        # status "canceled") while the row sat PAST_DUE and the member had NOT
        # chosen to cancel. That is an involuntary lapse, not a chosen cancel:
        # land EXPIRED so ``expired_at`` is stamped, the revival window opens,
        # and the member gets SUBSCRIPTION_EXPIRED (revive CTA) instead of a
        # cancellation confirmation (ADR-0014 / ADR-0015 dunning table).
        target_status = MembershipSubscription.SubscriptionStatus.EXPIRED.value
    if not target_status or subscription.status == target_status:
        return []
    changed = ["status"]
    subscription.status = target_status
    if target_status == MembershipSubscription.SubscriptionStatus.CANCELLED.value and not subscription.cancelled_at:
        subscription.cancelled_at = timezone.now()
        changed.append("cancelled_at")
    if target_status == MembershipSubscription.SubscriptionStatus.EXPIRED.value and not subscription.expired_at:
        subscription.expired_at = timezone.now()
        changed.append("expired_at")
    if target_status == MembershipSubscription.SubscriptionStatus.ACTIVE.value and subscription.expired_at:
        # Back to ACTIVE (e.g. a revival row's Stripe sub confirming):
        # the old expiry is consumed — a future lapse must stamp a fresh
        # one so the revival window doesn't anchor on stale data.
        subscription.expired_at = None
        changed.append("expired_at")
    return changed


@transaction.atomic
def sync_subscription_from_stripe(
    stripe_subscription: dict[str, t.Any],
) -> MembershipSubscription | None:
    """Mirror Stripe Subscription state onto our local row.

    Used by ``customer.subscription.{created,updated,deleted}`` handlers.
    Returns ``None`` if we don't know the subscription locally — Stripe-side
    rows for unrelated Connect accounts are expected and silently ignored.
    """
    stripe_id = stripe_subscription.get("id")
    if not stripe_id:
        return None
    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .select_related("plan", "plan__tier", "organization", "user")
        .filter(stripe_subscription_id=stripe_id)
        .first()
    )
    if subscription is None:
        return None

    if subscription.is_terminal:
        # Terminal rows are frozen. A late/out-of-order event (e.g. a stale
        # ``updated(active)`` racing a deletion, or Stripe dunning noise after
        # a local grace expiry) must never un-terminalize: if the user has
        # since re-subscribed, reviving this row would trip the partial unique
        # index, 500 the webhook, and put Stripe's retry loop (and eventually
        # the whole endpoint) at risk (C3 in the 2026-06-10 reassessment).
        logger.info(
            "subscription_sync_ignored_terminal_row",
            subscription_id=str(subscription.pk),
            stripe_subscription_id=stripe_id,
            local_status=subscription.status,
            stripe_status=stripe_subscription.get("status"),
        )
        return subscription

    prior_status = subscription.status  # captured before mutations for D3 dispatch gates
    prior_cap = subscription.cancel_at_period_end

    update_fields: list[str] = []
    update_fields.extend(_apply_status_transition(subscription, stripe_subscription, prior_cap=prior_cap))

    cap = bool(stripe_subscription.get("cancel_at_period_end", False))
    if subscription.cancel_at_period_end != cap:
        subscription.cancel_at_period_end = cap
        update_fields.append("cancel_at_period_end")

    update_fields.extend(_apply_period_dates(subscription, stripe_subscription))
    # Detect a price swap (schedule phase transition or direct upgrade) and
    # re-point ``subscription.plan`` accordingly.
    update_fields.extend(_apply_stripe_price_swap(subscription, stripe_subscription))

    if update_fields:
        subscription.save(update_fields=[*update_fields, "updated_at"])
    if subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE.value:
        _ensure_active_member(subscription)
    _dispatch_sync_notifications(subscription, prior_status=prior_status, prior_cap=prior_cap)
    return subscription


def _apply_invoice_outcome(
    subscription: MembershipSubscription,
    *,
    succeeded: bool,
    period_start: datetime | None,
    period_end: datetime | None,
) -> None:
    """Mirror an invoice outcome onto the subscription row (caller holds the lock).

    Terminal rows are frozen here exactly as they are in
    :func:`sync_subscription_from_stripe`, :func:`_apply_stripe_price_swap` and
    ``subscription_service.record_payment``: a CANCELLED/EXPIRED subscription
    must never advance its period (ADR-0014). The payment row itself is still
    written by the caller — the money genuinely moved and Stripe took its
    application fee, so suppressing it would desync our ledger from Stripe — but
    the member is owed a refund, which is why the caller raises an incident.
    """
    if subscription.is_terminal:
        return
    if succeeded:
        # Mirror the period from the invoice line and revive PENDING/PAST_DUE.
        update_fields: list[str] = []
        if period_start is not None and subscription.current_period_start != period_start:
            subscription.current_period_start = period_start
            update_fields.append("current_period_start")
        if period_end is not None and subscription.current_period_end != period_end:
            subscription.current_period_end = period_end
            update_fields.append("current_period_end")
        revivable = {
            MembershipSubscription.SubscriptionStatus.PENDING.value,
            MembershipSubscription.SubscriptionStatus.PAST_DUE.value,
        }
        if subscription.status in revivable:
            subscription.status = MembershipSubscription.SubscriptionStatus.ACTIVE
            update_fields.append("status")
        if subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE.value and subscription.expired_at:
            # An ONLINE revival row re-enters ACTIVE here (PENDING → first
            # invoice.paid). Clear the consumed expiry so a future lapse opens
            # a fresh revival window instead of anchoring on the stale one.
            subscription.expired_at = None
            update_fields.append("expired_at")
        if update_fields:
            subscription.save(update_fields=[*update_fields, "updated_at"])
        if subscription.status == MembershipSubscription.SubscriptionStatus.ACTIVE.value:
            # Guarded: a payment recorded against a non-revivable (e.g.
            # EXPIRED) row must not mint an ACTIVE OrganizationMember.
            _ensure_active_member(subscription)
        return
    # Mirror PAST_DUE; the grace-expiry Celery task takes over from here.
    if subscription.status in {
        MembershipSubscription.SubscriptionStatus.ACTIVE.value,
        MembershipSubscription.SubscriptionStatus.PENDING.value,
    }:
        subscription.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
        subscription.save(update_fields=["status", "updated_at"])


def _line_price_id(line: dict[str, t.Any]) -> str:
    """Best-effort price id for an invoice line, across dahlia and legacy shapes."""
    pricing = line.get("pricing") or {}
    details = pricing.get("price_details") or {}
    if details.get("price"):
        return t.cast(str, details["price"])
    price = line.get("price") or {}
    return t.cast(str, price.get("id") or "")


def _recurring_line_period(lines_data: list[dict[str, t.Any]], plan_price_id: str) -> dict[str, t.Any] | None:
    """Pick the billing period from the *recurring* line of an invoice.

    Stripe sorts invoice lines with "pending invoice items (including
    prorations)" first, so ``lines.data[0]`` is the proration line on any invoice
    carrying one — and a proration's period starts when it was calculated, not at
    the billing anchor. Writing that onto ``current_period_start`` corrupts the
    anchor ``_is_full_refund_of_current_period`` compares against, which silently
    suppresses the refund auto-cancel.

    Prefer the line whose price matches the plan, then the last non-proration
    line. Returns ``None`` when the invoice carries *only* prorations (a
    mid-cycle upgrade under ``always_invoice``): that invoice describes no
    billing period, so the caller must leave the subscription's anchor alone and
    let ``customer.subscription.updated`` supply the authoritative one.

    Args:
        lines_data: ``invoice["lines"]["data"]``.
        plan_price_id: The subscription plan's current Stripe price id.

    Returns:
        The chosen line's ``period`` mapping, or ``None`` when no recurring line
        is present.
    """
    if not lines_data:
        return None
    if plan_price_id:
        for line in lines_data:
            if _line_price_id(line) == plan_price_id and not line.get("proration"):
                return t.cast(dict[str, t.Any], line.get("period") or {})
    non_proration = [line for line in lines_data if not line.get("proration")]
    if non_proration:
        return t.cast(dict[str, t.Any], non_proration[-1].get("period") or {})
    return None


def _raise_payment_incidents(
    subscription: MembershipSubscription,
    payment: MembershipPayment,
    *,
    invoice_id: str,
    currency: str,
) -> None:
    """Emit money-correctness incidents for a successfully recorded payment.

    Both cases below are unrecoverable without a human: neither the nightly
    reconcile nor a Stripe redelivery repairs them.

    Args:
        subscription: The subscription the invoice belongs to.
        payment: The ledger row just written.
        invoice_id: The Stripe invoice id.
        currency: Payment currency.
    """
    if not payment.stripe_payment_intent_id:
        stripe_incidents.record_subscription_payment_intent_unresolved(
            subscription_id=str(subscription.pk),
            stripe_invoice_id=invoice_id,
        )
    if subscription.is_terminal:
        # Money changed hands for a period the member will never receive, and
        # the row is frozen by ``_apply_invoice_outcome`` so nothing heals it.
        stripe_incidents.record_subscription_paid_while_terminal(
            subscription_id=str(subscription.pk),
            status=subscription.status,
            stripe_invoice_id=invoice_id,
            payment_intent_id=payment.stripe_payment_intent_id,
            amount=str(payment.amount),
            currency=currency,
        )


@transaction.atomic
def record_stripe_payment_from_invoice(
    invoice: dict[str, t.Any],
    *,
    succeeded: bool,
) -> MembershipPayment | None:
    """Create or update a :class:`MembershipPayment` from a Stripe Invoice event.

    Args:
        invoice: The Stripe ``invoice.*`` event's ``data.object``.
        succeeded: ``True`` for ``invoice.paid``; ``False`` for
            ``invoice.payment_failed``.

    Returns:
        The created/updated :class:`MembershipPayment`, or ``None`` when the
        invoice belongs to a Stripe Subscription we don't know.
    """
    stripe_sub_id = _invoice_subscription_id(invoice)
    invoice_id = invoice.get("id")
    if not stripe_sub_id or not invoice_id:
        return None

    # Resolve everything that may hit the network BEFORE taking the row lock
    # (same discipline as the ticket-refund path / #632 reserve-session split):
    # _invoice_payment_intent_id can fall back to a stripe.Invoice.retrieve,
    # and with the pinned dahlia API version the legacy ``payment_intent``
    # field is absent, so that fallback fires on essentially every renewal.
    unlocked_subscription = (
        MembershipSubscription.objects.select_related("organization")
        .filter(stripe_subscription_id=stripe_sub_id)
        .first()
    )
    if unlocked_subscription is None:
        return None
    payment_intent_id = _invoice_payment_intent_id(invoice, unlocked_subscription.organization)

    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .select_related("plan", "plan__tier", "organization")
        .filter(stripe_subscription_id=stripe_sub_id)
        .first()
    )
    if subscription is None:
        return None

    prior_status = subscription.status  # captured before mutations for D3 dispatch gates

    currency_code = t.cast(str, invoice.get("currency") or subscription.plan.currency).upper()
    # SUCCEEDED: amount_paid (what changed hands). FAILED: 0, raw_response keeps the attempted amount.
    if succeeded:
        amount_minor = int(invoice.get("amount_paid") or 0)
    else:
        amount_minor = 0
    amount = from_stripe_amount(amount_minor, currency_code) if amount_minor else Decimal("0")

    # Stripe's ``application_fee_amount`` is what Revel actually collected — the
    # VAT-grossed ``application_fee_percent`` set at Checkout time — so it is the
    # gross fee and gets decomposed back into net + VAT for our accounting.
    fee_minor = int(invoice.get("application_fee_amount") or 0) if succeeded else 0
    fee_gross = from_stripe_amount(fee_minor, currency_code) if fee_minor else Decimal("0.00")
    if fee_gross:
        site = SiteSettings.get_solo()
        fee_breakdown = b2b_fee_vat_from_gross(
            fee_gross, subscription.organization, site.platform_vat_country, site.platform_vat_rate
        )
        fee_fields: dict[str, t.Any] = {
            "platform_fee": fee_breakdown.fee_gross,
            "platform_fee_net": fee_breakdown.fee_net,
            "platform_fee_vat": fee_breakdown.fee_vat,
            "platform_fee_vat_rate": fee_breakdown.fee_vat_rate,
            "platform_fee_reverse_charge": fee_breakdown.reverse_charge,
        }
    else:
        fee_fields = {
            "platform_fee": Decimal("0.00"),
            "platform_fee_net": None,
            "platform_fee_vat": None,
            "platform_fee_vat_rate": None,
            "platform_fee_reverse_charge": False,
        }

    lines_data = (invoice.get("lines") or {}).get("data") or []
    recurring_period = _recurring_line_period(lines_data, subscription.plan.stripe_price_id)
    # The payment row records what this invoice actually covered — for a
    # proration-only invoice that is the proration window, which is correct.
    ledger_period = (
        recurring_period
        if recurring_period is not None
        else ((lines_data[0].get("period") or {}) if lines_data else {})
    )
    period_start = _epoch_to_dt(ledger_period.get("start")) or timezone.now()
    period_end = _epoch_to_dt(ledger_period.get("end")) or timezone.now()

    if not succeeded:
        # Monotonicity guard: Stripe gives no delivery-order guarantee, and a
        # failed→retried→paid invoice emits both events. A late-arriving
        # ``payment_failed`` must never downgrade a payment row already
        # recorded as SUCCEEDED (the sub's status would self-heal via the
        # nightly reconcile, but the corrupted ledger row would not).
        existing = MembershipPayment.objects.filter(
            stripe_invoice_id=invoice_id,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
        ).first()
        if existing is not None:
            logger.info(
                "subscription_stripe_stale_payment_failed_ignored",
                subscription_id=str(subscription.pk),
                stripe_invoice_id=invoice_id,
            )
            return existing

    defaults: dict[str, t.Any] = {
        "subscription": subscription,
        "amount": amount,
        "currency": currency_code,
        "status": (MembershipPayment.PaymentStatus.SUCCEEDED if succeeded else MembershipPayment.PaymentStatus.FAILED),
        "period_start": period_start,
        "period_end": period_end,
        "raw_response": invoice,
        **fee_fields,
    }
    # An unresolved intent id must never overwrite one we already hold: it is the
    # only key ``charge.refunded`` matches membership payments on, and ONLINE
    # payments have no manual refund path to repair it with.
    if payment_intent_id:
        defaults["stripe_payment_intent_id"] = payment_intent_id

    # Capture the row's status BEFORE the upsert so we can tell a dunning/SCA
    # recovery (FAILED → SUCCEEDED on the SAME invoice, so ``created`` is False)
    # apart from a plain ``invoice.paid`` redelivery (already SUCCEEDED). We hold
    # the per-member lock, so no race with a concurrent write. Strictly == FAILED:
    # a redelivered paid invoice (SUCCEEDED) or a terminal-refunded row (REFUNDED)
    # must not count as a recovery.
    prior_payment_status = (
        MembershipPayment.objects.filter(stripe_invoice_id=invoice_id).values_list("status", flat=True).first()
    )

    payment, created = MembershipPayment.objects.update_or_create(
        stripe_invoice_id=invoice_id,
        defaults=defaults,
    )
    payment_recovered = succeeded and not created and prior_payment_status == MembershipPayment.PaymentStatus.FAILED

    if succeeded:
        _raise_payment_incidents(subscription, payment, invoice_id=invoice_id, currency=currency_code)

    _apply_invoice_outcome(
        subscription,
        succeeded=succeeded,
        # ``None`` for a proration-only invoice: it describes no billing period,
        # so the anchor stays put and ``customer.subscription.updated`` supplies it.
        period_start=period_start if recurring_period is not None else None,
        period_end=period_end if recurring_period is not None else None,
    )

    _dispatch_invoice_notifications(
        subscription,
        prior_status=prior_status,
        succeeded=succeeded,
        payment_created=created,
        payment_recovered=payment_recovered,
        billing_reason=t.cast(str, invoice.get("billing_reason") or ""),
    )

    logger.info(
        "subscription_stripe_payment_recorded",
        subscription_id=str(subscription.pk),
        stripe_invoice_id=invoice_id,
        succeeded=succeeded,
        amount=str(amount),
        currency=currency_code,
        created=created,
    )
    return payment
