"""Notification dispatch helpers for membership subscriptions.

Split out of :mod:`events.service.subscription_service` (file-length cap).
Private helpers called by OFFLINE dispatch sites (D2), ONLINE webhook
handlers (D3), and the renewal reminder task (E1). Each fires exactly one
notification via the ``notification_requested`` signal — the handler creates
the Notification row synchronously and defers the rendering/delivery task to
``transaction.on_commit`` (and MUST NOT raise, so a bad context can never
roll back a webhook transaction). Never mutates subscription state.

``subscription_service`` re-imports every helper, so callers keep using
``subscription_service._dispatch_*``.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from common.models import SiteSettings
from events.models import MembershipPayment, MembershipSubscription, MembershipSubscriptionPlan
from events.utils import format_organization_datetime
from notifications.enums import NotificationType
from notifications.signals import notification_requested


def _format_money(amount: t.Any, currency: str) -> str:
    """Format an amount with its currency for display in notifications."""
    return f"{amount} {currency}"


def _charged_money(
    plan: MembershipSubscriptionPlan,
    amount: Decimal | None,
    currency: str,
) -> str:
    """Format the amount actually charged, falling back to the plan's list price.

    ``plan.price`` is only an approximation of what a given member pays: a price
    change mints a NEW Stripe Price and leaves existing subscribers on the old
    one (grandfathering; ``migrate_plan_subscribers`` is the opt-in remedy), and
    OFFLINE payments carry whatever amount staff recorded. Callers that know the
    real figure pass it; ``None`` means "unknown" and keeps the old behaviour.

    The amount is quantized to the plan price's scale so a Stripe-derived figure
    (``from_stripe_amount`` divides, dropping trailing zeros: 1000 → ``10``)
    renders like every other money string members see.
    """
    if amount is None:
        return _format_money(plan.price, plan.currency)
    return _format_money(amount.quantize(plan.price), currency or plan.currency)


def last_paid_amounts(subscriptions: t.Iterable[MembershipSubscription]) -> dict[t.Any, Decimal]:
    """Map subscription id → the amount of its last real (non-proration) payment.

    Single query (Postgres ``DISTINCT ON``) so callers can resolve a whole
    cohort without an N+1. Mirrors the anchor ``migrate_plan_subscribers`` uses:
    proration invoices from a mid-cycle upgrade are a partial-period delta, not
    the subscriber's per-period price, so they can never anchor a quote.
    """
    return dict(
        MembershipPayment.objects.filter(
            subscription__in=subscriptions,
            status=MembershipPayment.PaymentStatus.SUCCEEDED,
        )
        .exclude(raw_response__contains={"billing_reason": "subscription_update"})
        .order_by("subscription_id", "-created_at")
        .distinct("subscription_id")
        .values_list("subscription_id", "amount")
    )


def _common_subscription_context(subscription: MembershipSubscription) -> dict[str, t.Any]:
    """Base context shared by all subscription notifications.

    Includes an absolute ``organization_contact_url`` so email and Telegram
    templates render clickable links instead of relative paths that break in
    those clients. For ONLINE (Stripe-managed) subscriptions it also carries a
    ``manage_subscription_url`` pointing at the member's subscription page,
    from which the frontend calls ``POST /billing-portal`` to mint a Stripe
    Customer Portal session on demand. We deliberately do **not** create a
    portal session here: dispatch runs inside a locked webhook transaction and
    must issue no Stripe network calls.
    """
    org = subscription.organization
    plan = subscription.plan
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    ctx: dict[str, t.Any] = {
        "organization_name": org.name,
        "organization_slug": org.slug,
        "plan_name": plan.name,
        "organization_contact_url": f"{frontend_base_url}/org/{org.slug}/contact",
    }
    if plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE.value:
        ctx["manage_subscription_url"] = f"{frontend_base_url}/org/{org.slug}/subscription"
    return ctx


def _dispatch_renewal_succeeded(
    subscription: MembershipSubscription,
    *,
    amount: Decimal | None = None,
    currency: str = "",
) -> None:
    """Fire SUBSCRIPTION_RENEWAL_SUCCEEDED for a renewal payment.

    ``amount``/``currency`` are what actually changed hands (the Stripe
    invoice's ``amount_paid``, or the amount staff recorded offline). Left
    unset, the notification quotes the plan's current list price, which is
    wrong for a grandfathered subscriber — pass them whenever known.
    """
    plan = subscription.plan
    ctx = _common_subscription_context(subscription)
    ctx.update(
        amount=_charged_money(plan, amount, currency),
        period_end=(subscription.current_period_end.date().isoformat() if subscription.current_period_end else ""),
    )
    notification_requested.send(
        sender=MembershipSubscription,
        user=subscription.user,
        notification_type=NotificationType.SUBSCRIPTION_RENEWAL_SUCCEEDED,
        context=ctx,
    )


def _dispatch_payment_failed(
    subscription: MembershipSubscription,
    *,
    grace_period_end: t.Any,
    is_online: bool,
    amount: Decimal | None = None,
    currency: str = "",
) -> None:
    """Fire SUBSCRIPTION_PAYMENT_FAILED when a renewal payment fails.

    For ONLINE subscriptions the context carries ``manage_subscription_url``
    (built in :func:`_common_subscription_context`), which the payment-failed
    templates surface as an "Update Payment Method" CTA.

    ``amount``/``currency`` are the sum at stake (the failed invoice's
    ``amount_due``); unset falls back to the plan's list price, which a
    grandfathered subscriber does not owe.
    """
    plan = subscription.plan
    ctx = _common_subscription_context(subscription)
    ctx.update(
        amount=_charged_money(plan, amount, currency),
        # Org-local, human-readable — never raw UTC isoformat (#511/#542).
        grace_period_end=format_organization_datetime(grace_period_end, subscription.organization),
        is_online=is_online,
    )
    notification_requested.send(
        sender=MembershipSubscription,
        user=subscription.user,
        notification_type=NotificationType.SUBSCRIPTION_PAYMENT_FAILED,
        context=ctx,
    )


def _dispatch_subscription_expired(subscription: MembershipSubscription) -> None:
    """Fire SUBSCRIPTION_EXPIRED with a revival CTA if within window."""
    org = subscription.organization
    revival_window_end: t.Any = None
    revival_url: str | None = None
    if subscription.expired_at and org.membership_subscription_revival_window_days > 0:
        revival_window_end = subscription.expired_at + timedelta(days=org.membership_subscription_revival_window_days)
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        revival_url = f"{frontend_base_url}/org/{org.slug}/subscription/revive"
    ctx = _common_subscription_context(subscription)
    # Org-local, human-readable — never raw UTC isoformat (#511/#542).
    ctx["expired_at"] = format_organization_datetime(subscription.expired_at, org)
    if revival_window_end is not None:
        ctx["revival_window_end"] = format_organization_datetime(revival_window_end, org)
    if revival_url is not None:
        ctx["revival_url"] = revival_url
    notification_requested.send(
        sender=MembershipSubscription,
        user=subscription.user,
        notification_type=NotificationType.SUBSCRIPTION_EXPIRED,
        context=ctx,
    )


def _dispatch_cancellation_confirmed(subscription: MembershipSubscription, *, immediate: bool) -> None:
    """Fire SUBSCRIPTION_CANCELLATION_CONFIRMED for cancel-now or cancel-at-period-end."""
    if immediate:
        access_ends_at = timezone.now()
    else:
        access_ends_at = subscription.current_period_end or timezone.now()
    ctx = _common_subscription_context(subscription)
    ctx.update(
        immediate=immediate,
        # Org-local, human-readable — never raw UTC isoformat (#511/#542).
        access_ends_at=format_organization_datetime(access_ends_at, subscription.organization),
    )
    notification_requested.send(
        sender=MembershipSubscription,
        user=subscription.user,
        notification_type=NotificationType.SUBSCRIPTION_CANCELLATION_CONFIRMED,
        context=ctx,
    )


def _dispatch_revival_checkout(subscription: MembershipSubscription, *, checkout_url: str) -> None:
    """Fire SUBSCRIPTION_REVIVAL_CHECKOUT with the hosted Checkout link.

    Sent when staff revive a member's ONLINE subscription: staff cannot pay on
    the member's behalf, so the member gets the checkout link to complete the
    renewal themselves.
    """
    plan = subscription.plan
    ctx = _common_subscription_context(subscription)
    ctx.update(
        amount=_format_money(plan.price, plan.currency),
        checkout_url=checkout_url,
    )
    notification_requested.send(
        sender=MembershipSubscription,
        user=subscription.user,
        notification_type=NotificationType.SUBSCRIPTION_REVIVAL_CHECKOUT,
        context=ctx,
    )


def _dispatch_price_migration(
    subscription: MembershipSubscription,
    *,
    old_price: t.Any,
    new_price: t.Any,
) -> None:
    """Fire SUBSCRIPTION_PRICE_MIGRATION_NOTICE."""
    plan = subscription.plan
    ctx = _common_subscription_context(subscription)
    ctx.update(
        old_amount=_format_money(old_price, plan.currency),
        new_amount=_format_money(new_price, plan.currency),
        effective_at=(subscription.current_period_end.date().isoformat() if subscription.current_period_end else ""),
    )
    notification_requested.send(
        sender=MembershipSubscription,
        user=subscription.user,
        notification_type=NotificationType.SUBSCRIPTION_PRICE_MIGRATION_NOTICE,
        context=ctx,
    )
