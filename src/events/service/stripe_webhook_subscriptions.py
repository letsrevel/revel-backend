"""Membership-subscription handlers for the Stripe webhook dispatcher.

Mixin methods for :class:`events.service.stripe_webhooks.StripeEventHandler`,
split out to keep that module under the file-length cap. The dispatch map and
dedup gate stay in ``stripe_webhooks``; these methods mirror Stripe
subscription/invoice state onto the local rows via ``subscription_stripe_sync``
and route full charge refunds through ``subscription_refunds``.
"""

import typing as t
import uuid

import stripe
import structlog

from events.models import MembershipPayment, MembershipSubscription

logger = structlog.get_logger(__name__)


class SubscriptionWebhookHandlersMixin:
    """Membership subscription webhook handlers (Phases 2-4)."""

    def handle_subscription_checkout_completed(self, event: stripe.Event) -> None:
        """Link the Stripe Subscription a completed Checkout Session created.

        In ``mode=subscription`` the Stripe Subscription only exists once the
        session completes, so the local PENDING row (created at session mint
        time with ``stripe_checkout_session_id`` set) has no
        ``stripe_subscription_id`` until this event. Everything downstream
        (``customer.subscription.*`` sync, ``invoice.paid`` → ACTIVE + member
        grant) keys off that id, so this link is what plugs the row into the
        existing pipeline.

        Sessions without our ``membership_subscription_id`` metadata are
        ignored — orgs can run their own subscription checkouts on the same
        Connect account.
        """
        session = event.data.object
        raw_id = (session.get("metadata") or {}).get("membership_subscription_id")
        if not raw_id:
            logger.info("subscription_checkout_completed_no_metadata", session_id=session.get("id"))
            return
        try:
            subscription_pk = uuid.UUID(raw_id)
        except ValueError:
            logger.warning(
                "subscription_checkout_completed_bad_metadata",
                session_id=session.get("id"),
                membership_subscription_id=raw_id,
            )
            return

        subscription = MembershipSubscription.objects.select_for_update().filter(pk=subscription_pk).first()
        if subscription is None:
            logger.warning(
                "subscription_checkout_completed_unknown_row",
                session_id=session.get("id"),
                membership_subscription_id=raw_id,
            )
            return

        stripe_sub = session.get("subscription")
        stripe_sub_id = stripe_sub.get("id") if isinstance(stripe_sub, dict) else stripe_sub
        if not stripe_sub_id:
            logger.warning(
                "subscription_checkout_completed_missing_subscription",
                session_id=session.get("id"),
                subscription_id=str(subscription.pk),
            )
            return
        if subscription.stripe_subscription_id == stripe_sub_id:
            return  # Idempotent redelivery.

        subscription.stripe_subscription_id = stripe_sub_id
        subscription.save(update_fields=["stripe_subscription_id", "updated_at"])
        logger.info(
            "subscription_checkout_completed_linked",
            session_id=session.get("id"),
            subscription_id=str(subscription.pk),
            stripe_subscription_id=stripe_sub_id,
        )
        # Out-of-order delivery guard: Stripe guarantees no event ordering, and
        # for ``mode=subscription`` the first ``invoice.paid`` fires almost
        # simultaneously with this event. If the invoice event arrived FIRST it
        # found no local row carrying this ``stripe_subscription_id`` and was
        # silently dropped (its dedup row committed as HANDLED, so Stripe never
        # retries it) — losing the member's first payment from the ledger and
        # leaving the row PENDING. Replay the already-paid initial invoice now.
        self._backfill_initial_invoice(session, subscription)

    @staticmethod
    def _backfill_initial_invoice(session: t.Any, subscription: MembershipSubscription) -> None:
        """Record the checkout's first invoice if its ``invoice.paid`` was dropped.

        No-op when a payment already exists for the subscription (the normal
        ordering), when the session carries no invoice reference, or when the
        invoice isn't paid yet (async payment methods — the later
        ``invoice.paid`` now resolves against the freshly-linked row). The
        ``Invoice.retrieve`` runs while holding this member's row lock — the
        documented single-member-blast-radius trade for webhook paths — and a
        Stripe failure propagates so the whole delivery (including the link and
        the dedup row) rolls back and Stripe redelivers.
        """
        from events.service import subscription_stripe_sync
        from events.service.subscription_stripe_payloads import _stripe_account_kwargs

        if MembershipPayment.objects.filter(subscription=subscription).exists():
            return
        invoice_ref = session.get("invoice")
        invoice_id = invoice_ref.get("id") if isinstance(invoice_ref, dict) else invoice_ref
        if not invoice_id:
            return
        invoice = stripe.Invoice.retrieve(invoice_id, **_stripe_account_kwargs(subscription.organization))
        if invoice.get("status") != "paid":
            return
        subscription_stripe_sync.record_stripe_payment_from_invoice(dict(invoice), succeeded=True)
        logger.info(
            "subscription_checkout_initial_invoice_backfilled",
            subscription_id=str(subscription.pk),
            stripe_invoice_id=invoice_id,
        )

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
