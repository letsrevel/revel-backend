"""Membership-subscription handlers for the Stripe webhook dispatcher.

Mixin methods for :class:`events.service.stripe_webhooks.StripeEventHandler`,
split out to keep that module under the file-length cap. The dispatch map and
dedup gate stay in ``stripe_webhooks``; these methods mirror Stripe
subscription/invoice state onto the local rows via ``subscription_stripe_sync``
and route full charge refunds through ``subscription_refunds``.
"""

import stripe
import structlog

from events.models import MembershipPayment

logger = structlog.get_logger(__name__)


class SubscriptionWebhookHandlersMixin:
    """Membership subscription webhook handlers (Phases 2-4)."""

    def _handle_subscription_refund(
        self,
        event: stripe.Event,
        membership_payment: MembershipPayment,
    ) -> None:
        """Apply a charge.refunded event to a MembershipPayment.

        Delegates to subscription_refunds.refund_payment, which marks the row
        REFUNDED and (if the refund fully covers the current period) cancels
        the subscription immediately — the Stripe-side cancel is deferred to
        after commit so no network call happens under the row locks held here.

        Idempotent on already-REFUNDED rows. Partial refunds are ignored —
        ``charge.refunded`` fires for both partial and full refunds, but only
        a fully refunded charge should flip the MembershipPayment to REFUNDED
        and trigger the auto-cancel path.

        Args:
            event: The Stripe webhook event.
            membership_payment: The MembershipPayment row matched by payment_intent_id.
        """
        from events.service import subscription_refunds

        if membership_payment.status == MembershipPayment.PaymentStatus.REFUNDED:
            # Idempotent: re-delivered webhook for an already-processed refund.
            return

        charge_data = event.data.object
        amount = charge_data.get("amount")
        amount_refunded = charge_data.get("amount_refunded")
        if amount is None or amount_refunded is None or amount_refunded < amount:
            logger.info(
                "stripe_subscription_partial_refund_ignored",
                payment_intent_id=charge_data.get("payment_intent"),
                membership_payment_id=str(membership_payment.id),
                amount=amount,
                amount_refunded=amount_refunded,
            )
            return

        subscription_refunds.refund_payment(membership_payment, recorded_by=None)

        logger.info(
            "stripe_subscription_refund_processed",
            payment_intent_id=event.data.object.get("payment_intent"),
            membership_payment_id=str(membership_payment.id),
            subscription_id=str(membership_payment.subscription_id),
        )

    def handle_customer_subscription_created(self, event: stripe.Event) -> None:
        """Mirror Stripe Subscription state when Stripe confirms creation."""
        from events.service import subscription_stripe_sync

        subscription_stripe_sync.sync_subscription_from_stripe(dict(event.data.object))

    def handle_customer_subscription_updated(self, event: stripe.Event) -> None:
        """Mirror status / period / cancel_at_period_end onto the local row."""
        from events.service import subscription_stripe_sync

        subscription_stripe_sync.sync_subscription_from_stripe(dict(event.data.object))

    def handle_customer_subscription_deleted(self, event: stripe.Event) -> None:
        """Stripe-side cancellation (immediate or end-of-period) — mark terminal."""
        from events.service import subscription_stripe_sync

        subscription_stripe_sync.sync_subscription_from_stripe(dict(event.data.object))

    def handle_invoice_paid(self, event: stripe.Event) -> None:
        """Record a SUCCEEDED MembershipPayment + revive PENDING/PAST_DUE → ACTIVE."""
        from events.service import subscription_stripe_sync

        subscription_stripe_sync.record_stripe_payment_from_invoice(dict(event.data.object), succeeded=True)

    def handle_invoice_payment_failed(self, event: stripe.Event) -> None:
        """Record a FAILED MembershipPayment + transition subscription to PAST_DUE."""
        from events.service import subscription_stripe_sync

        subscription_stripe_sync.record_stripe_payment_from_invoice(dict(event.data.object), succeeded=False)

    def handle_invoice_payment_action_required(self, event: stripe.Event) -> None:
        """Handle an off-session renewal blocked on SCA/3DS confirmation.

        The invoice stays open (no immediate ``payment_failed``), so without
        this hook an SCA-stuck renewal would surface only via a much later
        failure event or the nightly reconcile. Routing through the failed
        branch transitions the sub to PAST_DUE and fires the payment-failed
        dunning notification (whose portal link is where the member completes
        the confirmation). The monotonicity guard in
        ``record_stripe_payment_from_invoice`` keeps a later ``invoice.paid``
        (or an out-of-order one) authoritative for the ledger.
        """
        from events.service import subscription_stripe_sync

        subscription_stripe_sync.record_stripe_payment_from_invoice(dict(event.data.object), succeeded=False)
