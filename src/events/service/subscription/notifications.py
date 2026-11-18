"""Notification dispatch helpers for membership subscriptions.

Split out of :mod:`events.service.subscription.lifecycle` (file-length cap).
Private helpers called by OFFLINE dispatch sites (D2), ONLINE webhook
handlers (D3), and the renewal reminder task (E1). Each fires exactly one
notification via the ``notification_requested`` signal — the handler creates
the Notification row synchronously and defers the rendering/delivery task to
``transaction.on_commit`` (and MUST NOT raise, so a bad context can never
roll back a webhook transaction). Never mutates subscription state.

Callers import this module directly (``from events.service.subscription import
notifications``) and go through ``notifications._dispatch_*`` so the helpers stay
patchable in tests.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

from django.db.models.functions import Coalesce
from django.utils import timezone

from common.models import SiteSettings
from events.models import MembershipPayment, MembershipSubscription, MembershipSubscriptionPlan
from events.utils import format_organization_datetime
from events.utils.frontend_urls import frontend_base_url_for
from events.utils.subscription_payment import APPLICATION_PAYMENT_WINDOW_DAYS, is_never_paid_on_offline_plan
from events.utils.subscription_periods import first_period_end, first_period_price
from notifications.enums import NotificationType
from notifications.service.notification_helpers import notify_org_staff
from notifications.signals import notification_requested

#: The one member-facing subscription surface in the frontend (#815). It lists every
#: live membership (manage / cancel, which is where the Stripe billing portal is
#: opened from) plus the rejoin CTA for expired-but-in-window ONLINE subscriptions,
#: so it answers all seven subscription notifications' implied next action. Per-org
#: deep links such as ``/org/<slug>/subscription`` and ``/org/<slug>/contact`` are
#: not routes and render a 404 (there is no catch-all) — do not reintroduce them
#: without a corresponding frontend route. Only ``/org/<slug>`` itself exists.
MEMBERSHIPS_PATH = "/account/memberships"


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
    """Map subscription id → the per-period price anchored by its last real (non-proration) payment.

    This is the one anchor query: MRR, price-migration notices and renewal reminders all use it.
    A prorated calendar stub anchors on the full price it stood in for (``full_period_amount``).

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
        .values_list("subscription_id", Coalesce("full_period_amount", "amount"))
    )


def _common_subscription_context(subscription: MembershipSubscription) -> dict[str, t.Any]:
    """Base context shared by all subscription notifications.

    Includes an absolute ``organization_contact_url`` so email and Telegram
    templates render clickable links instead of relative paths that break in
    those clients. It points at the organization's public page — the frontend
    renders the contact form inline there and has no ``/org/<slug>/contact``
    route (see :data:`MEMBERSHIPS_PATH`). For OFFLINE plans this is the only
    CTA in the payment-failed and renewal-reminder templates, so it must
    resolve. For ONLINE (Stripe-managed) subscriptions it also carries a
    ``manage_subscription_url`` pointing at :data:`MEMBERSHIPS_PATH`, from which
    the frontend calls ``POST /billing-portal`` to mint a Stripe Customer Portal
    session on demand. We deliberately do **not** create a portal session here:
    dispatch runs inside a locked webhook transaction and must issue no Stripe
    network calls.
    """
    org = subscription.organization
    plan = subscription.plan
    ctx: dict[str, t.Any] = {
        "organization_id": str(org.id),
        "organization_name": org.name,
        "organization_slug": org.slug,
        "plan_name": plan.name,
        "organization_contact_url": f"{frontend_base_url_for(org)}/org/{org.slug}",
    }
    if plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE.value:
        # /account/* is the platform account area, not an org route: it never moves to a tenant domain.
        ctx["manage_subscription_url"] = f"{SiteSettings.get_solo().frontend_base_url}{MEMBERSHIPS_PATH}"
    return ctx


def dispatch_membership_payment_requested(
    subscription: MembershipSubscription, *, application_accepted: bool = False
) -> None:
    """Fire MEMBERSHIP_PAYMENT_REQUESTED: an OFFLINE plan was opened, the member must pay the organization directly.

    Public (no leading underscore): called from the offline-join flow rather than the
    webhook/lifecycle sites the other helpers serve. Fires through the signal like they do.
    ``application_accepted`` leads the message with "<org> accepted your application." (the organization
    already accepted it; a member-facing join that self-approves its own application is not an acceptance).
    """
    plan = subscription.plan
    ctx = _common_subscription_context(subscription)
    ctx.update(
        tier_name=plan.tier.name,
        amount=_charged_money(plan, None, ""),
        period_label=plan.get_period_unit_display(),
        payment_instructions=plan.effective_payment_instructions or "",
        # /account/* is the platform account area, never an org (tenant) route.
        memberships_url=f"{SiteSettings.get_solo().frontend_base_url}{MEMBERSHIPS_PATH}",
        application_accepted=application_accepted,
        payment_window_days=APPLICATION_PAYMENT_WINDOW_DAYS,
    )
    notification_requested.send(
        sender=MembershipSubscription,
        user=subscription.user,
        notification_type=NotificationType.MEMBERSHIP_PAYMENT_REQUESTED,
        context=ctx,
    )


def dispatch_membership_offline_joined(subscription: MembershipSubscription) -> int:
    """Tell org staff someone joined an OFFLINE plan directly (no application) and now owes a payment.

    Returns the number of staff notified.
    """
    org = subscription.organization
    user = subscription.user
    frontend_url = f"{frontend_base_url_for(org)}/org/{org.slug}/admin/members?tab=subscriptions&status=pending"
    return notify_org_staff(
        organization_id=org.id,
        notification_type=NotificationType.MEMBERSHIP_OFFLINE_JOINED,
        context={
            "subscription_id": str(subscription.id),
            "organization_id": str(org.id),
            "organization_name": org.name,
            "member_id": str(user.id),
            "member_name": user.display_name,
            "member_email": user.email,
            "tier_name": subscription.plan.tier.name,
            "plan_name": subscription.plan.name,
            "frontend_url": frontend_url,
        },
        sender=MembershipSubscription,
    )


def _dispatch_renewal_succeeded(
    subscription: MembershipSubscription,
    *,
    amount: Decimal | None = None,
    currency: str = "",
    next_charge_amount: Decimal | None = None,
) -> None:
    """Fire SUBSCRIPTION_RENEWAL_SUCCEEDED for a renewal payment.

    ``amount``/``currency`` are what actually changed hands (the Stripe
    invoice's ``amount_paid``, or the amount staff recorded offline). Left
    unset, the notification quotes the plan's current list price, which is
    wrong for a grandfathered subscriber — pass them whenever known.

    ``next_charge_amount`` is set for the first (stub) payment of a calendar plan: a short stub can
    be followed by the full charge within hours, so the confirmation states it and its date.
    """
    plan = subscription.plan
    ctx = _common_subscription_context(subscription)
    ctx.update(
        amount=_charged_money(plan, amount, currency),
        period_end=(subscription.current_period_end.date().isoformat() if subscription.current_period_end else ""),
    )
    if next_charge_amount is not None and subscription.current_period_end:
        ctx.update(
            next_charge_amount=_format_money(next_charge_amount, plan.currency),
            next_charge_date=subscription.current_period_end.date().isoformat(),
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
        revival_url = f"{frontend_base_url}{MEMBERSHIPS_PATH}"
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


def dispatch_unpaid_offline_lapsed(subscription: MembershipSubscription) -> None:
    """Fire SUBSCRIPTION_EXPIRED (``lapsed`` branch): a never-paid offline request timed out and was cancelled."""
    ctx = _common_subscription_context(subscription)
    ctx["expired_at"] = format_organization_datetime(timezone.now(), subscription.organization)
    ctx["lapsed"] = True
    org = subscription.organization
    ctx["membership_url"] = f"{frontend_base_url_for(org)}/org/{org.slug}/membership"
    notification_requested.send(
        sender=MembershipSubscription,
        user=subscription.user,
        notification_type=NotificationType.SUBSCRIPTION_EXPIRED,
        context=ctx,
    )


def _dispatch_cancellation_confirmed(
    subscription: MembershipSubscription,
    *,
    immediate: bool,
    prior_status: str | None = None,
    rejoinable: bool = True,
) -> None:
    """Fire SUBSCRIPTION_CANCELLATION_CONFIRMED for cancel-now or cancel-at-period-end.

    ``prior_status`` is the status before the cancel (the row may already be CANCELLED): only a PENDING,
    never-paid OFFLINE row gets the "membership request cancelled" variant. ``rejoinable=False`` (a ban)
    drops the "you can join or apply again" line from that variant.
    """
    if immediate:
        access_ends_at = timezone.now()
    else:
        access_ends_at = subscription.current_period_end or timezone.now()
    ctx = _common_subscription_context(subscription)
    # PENDING and never paid on an OFFLINE plan: a withdrawn or declined request, not a membership ending.
    ctx["request_cancelled"] = (
        prior_status == MembershipSubscription.SubscriptionStatus.PENDING
        and is_never_paid_on_offline_plan(subscription)
    )
    ctx["rejoinable"] = rejoinable
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
    # A calendar plan's checkout charges the prorated stub up to the next boundary, not the full price.
    now = timezone.now()
    first_price = first_period_price(plan, now)
    first_end = first_period_end(plan, now)
    ctx.update(
        amount=_format_money(plan.price if first_price is None else first_price, plan.currency),
        checkout_url=checkout_url,
    )
    if first_end is not None:
        # The stub can be tiny (or free) and followed by the full charge within hours: say so up front.
        ctx.update(
            next_charge_amount=_format_money(plan.price, plan.currency),
            next_charge_date=first_end.date().isoformat(),
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
