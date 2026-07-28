"""Undo a scheduled cancellation on a membership subscription.

Split out of :mod:`events.service.subscription_service` and
:mod:`events.service.subscription_stripe_service` (both at the 1000-line
file-length cap). Holds the exact inverse of
``cancel_subscription(immediate=False)``: clearing ``cancel_at_period_end`` so
the subscription keeps renewing.

Nothing else moves — status, period and payment history are untouched — so this
is only ever a way *back* from a scheduled cancel, never a way to resurrect a
terminalized row (that is what revival is for).
"""

import stripe
import structlog
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import MembershipSubscription, MembershipSubscriptionPlan
from events.service.subscription_stripe_payloads import _stripe_account_kwargs

logger = structlog.get_logger(__name__)


def _uncancel_on_stripe(subscription: MembershipSubscription, stripe_subscription_id: str) -> None:
    """Clear ``cancel_at_period_end`` on the linked Stripe subscription.

    Mirrors :func:`subscription_stripe_service.pause_online_subscription`'s
    failure handling: any Stripe error surfaces as the module-wide retryable
    502 and leaves local state untouched, so we never record a renewal Stripe
    never accepted.
    """
    try:
        stripe.Subscription.modify(
            stripe_subscription_id,
            cancel_at_period_end=False,
            **_stripe_account_kwargs(subscription.organization),
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "subscription_stripe_uncancel_failed",
            subscription_id=str(subscription.pk),
            stripe_subscription_id=stripe_subscription_id,
            error=str(exc),
        )
        raise HttpError(502, str(_("Payment processing failed. Please try again later."))) from exc


@transaction.atomic
def uncancel_subscription(subscription: MembershipSubscription) -> MembershipSubscription:
    """Clear ``cancel_at_period_end`` so the subscription renews again.

    Refuses a terminal row — the cancellation already happened, so there is
    nothing left to undo — and a row whose plan has since been archived, with
    the same reason a fresh subscribe would give. A row that is not scheduled to
    cancel is already in the requested state and returns unchanged, mirroring
    :func:`subscription_service.pause_subscription`'s already-PAUSED no-op.

    For ONLINE (Stripe-managed) subscriptions the flag is cleared on Stripe
    first; the ``customer.subscription.updated`` webhook then re-settles local
    state. As in ``cancel_online_subscription`` the flag is also mirrored
    locally right away so the caller need not wait for the webhook round-trip.
    An ONLINE row with no Stripe link yet (cancelled while its first checkout
    was still pending) takes the local path, exactly as the cancel side does.
    """
    # Same lock protocol as cancel/pause/resume: reload under a row lock that is
    # held across the Stripe call, which serializes this mutation against echo
    # webhooks (see docs/engineering-notes.md).
    subscription = (
        MembershipSubscription.objects.select_for_update(of=("self",))
        .select_related("plan", "plan__tier", "organization")
        .get(pk=subscription.pk)
    )
    if subscription.is_terminal:
        raise HttpError(400, str(_("Cannot resume renewal on a cancelled or expired subscription.")))
    if not subscription.cancel_at_period_end:
        return subscription
    if not subscription.plan.is_active:
        # Same refusal as ``create_subscription`` / ``_validate_revivable``:
        # keeping the renewal alive would go on billing a retired plan.
        raise HttpError(400, str(_("This plan is archived and no longer accepts new subscriptions.")))

    if (
        subscription.plan.payment_method == MembershipSubscriptionPlan.PaymentMethod.ONLINE
        and subscription.stripe_subscription_id
    ):
        _uncancel_on_stripe(subscription, subscription.stripe_subscription_id)

    subscription.cancel_at_period_end = False
    subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
    logger.info("subscription_uncancelled", subscription_id=str(subscription.pk))
    return subscription
