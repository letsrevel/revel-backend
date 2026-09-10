"""Membership subscription service.

This package groups the subscription service functions by concern — the local
state machine (``core``, ``lifecycle``, ``plans``, ``sales``, ``eligibility``,
``uncancel``, ``refunds``, ``reporting``, ``notifications``) and the Stripe
integration that backs ONLINE rows (the ``stripe`` subpackage). Every public
symbol is re-exported here, so a caller that only needs the domain API can
``from events.service import subscription`` and reach it without knowing which
module it lives in.

Call sites that patch or monkey-patch a helper should keep importing the owning
module (``from events.service.subscription.stripe import checkout``) — patching
a name here only affects lookups that go through this package.
"""

from events.service.subscription.core import (
    InitialPayment,
    create_subscription,
    record_payment,
)
from events.service.subscription.eligibility import (
    ensure_tier_change_allowed,
    subscribe_to_plan,
)
from events.service.subscription.lifecycle import (
    cancel_subscription,
    cancel_subscriptions_for_membership_loss,
    change_plan,
    pause_subscription,
    resume_subscription,
    revive_subscription,
)
from events.service.subscription.notifications import last_paid_amounts
from events.service.subscription.plans import (
    MigrationError,
    MigrationResult,
    archive_plan,
    create_plan,
    delete_plan,
    migrate_plan_subscribers,
    update_plan,
)
from events.service.subscription.refunds import refund_payment
from events.service.subscription.reporting import (
    StatusBreakdown,
    SubscriptionMetrics,
    get_organization_metrics,
    organization_payments,
)
from events.service.subscription.sales import (
    ensure_member_not_excluded,
    ensure_plan_on_sale,
    ensure_plan_sales_capacity,
)
from events.service.subscription.stripe.base import (
    ensure_stripe_price,
    require_stripe_connected,
)
from events.service.subscription.stripe.checkout import (
    archive_stripe_price,
    cancel_online_subscription,
    cancel_stripe_subscription_best_effort,
    classify_stale_pending_checkout,
    clear_stale_pending_checkout,
    create_billing_portal_session,
    create_revival_checkout,
    ensure_customer_profile,
    pause_online_subscription,
    resume_online_subscription,
    start_online_subscription,
    update_subscription_price,
)
from events.service.subscription.stripe.fees import (
    FeeResyncCounters,
    effective_application_fee_percent,
    resync_subscription_application_fees,
)
from events.service.subscription.stripe.payloads import (
    InvoicePaymentDetails,
    StripeAccountKwargs,
    epoch_to_dt,
    invoice_payment_details,
    invoice_subscription_id,
    is_subscription_gone,
    stripe_account_kwargs,
    stripe_interval,
    subscription_period_epochs,
)
from events.service.subscription.stripe.plan_change import (
    change_online_plan,
    release_online_schedule,
    resolve_refused_cancel,
)
from events.service.subscription.stripe.sync import (
    map_stripe_status,
    record_stripe_payment_from_invoice,
    settle_originating_application,
    sync_subscription_from_stripe,
)
from events.service.subscription.uncancel import uncancel_subscription

__all__ = [
    "FeeResyncCounters",
    "InitialPayment",
    "InvoicePaymentDetails",
    "MigrationError",
    "MigrationResult",
    "StatusBreakdown",
    "StripeAccountKwargs",
    "SubscriptionMetrics",
    "archive_plan",
    "archive_stripe_price",
    "cancel_online_subscription",
    "cancel_stripe_subscription_best_effort",
    "cancel_subscription",
    "cancel_subscriptions_for_membership_loss",
    "change_online_plan",
    "change_plan",
    "classify_stale_pending_checkout",
    "clear_stale_pending_checkout",
    "create_billing_portal_session",
    "create_plan",
    "create_revival_checkout",
    "create_subscription",
    "delete_plan",
    "effective_application_fee_percent",
    "ensure_customer_profile",
    "ensure_member_not_excluded",
    "ensure_plan_on_sale",
    "ensure_plan_sales_capacity",
    "ensure_stripe_price",
    "ensure_tier_change_allowed",
    "epoch_to_dt",
    "get_organization_metrics",
    "invoice_payment_details",
    "invoice_subscription_id",
    "is_subscription_gone",
    "last_paid_amounts",
    "map_stripe_status",
    "migrate_plan_subscribers",
    "organization_payments",
    "pause_online_subscription",
    "pause_subscription",
    "record_payment",
    "record_stripe_payment_from_invoice",
    "refund_payment",
    "release_online_schedule",
    "require_stripe_connected",
    "resolve_refused_cancel",
    "resume_online_subscription",
    "resume_subscription",
    "resync_subscription_application_fees",
    "revive_subscription",
    "settle_originating_application",
    "start_online_subscription",
    "stripe_account_kwargs",
    "stripe_interval",
    "subscribe_to_plan",
    "subscription_period_epochs",
    "sync_subscription_from_stripe",
    "uncancel_subscription",
    "update_plan",
    "update_subscription_price",
]
