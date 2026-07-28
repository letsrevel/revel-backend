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
from django.conf import settings
from django.utils import timezone

from events.models import MembershipPayment, MembershipSubscription
from events.service import stripe_incidents
from events.utils.currency import from_stripe_amount

logger = structlog.get_logger(__name__)


class SubscriptionWebhookHandlersMixin:
    """Membership subscription webhook handlers (Phases 2-4)."""

    if t.TYPE_CHECKING:
        # Provided by the host StripeEventHandler; declared for mypy only.
        def _resolve_refunds(self, charge_data: dict[str, t.Any]) -> list[dict[str, t.Any]]: ...

    @staticmethod
    def _event_account_owns(event: stripe.Event, subscription: MembershipSubscription) -> bool:
        """Whether this delivery's connected account owns ``subscription``.

        Mirrors :func:`_stripe_account_kwargs`' host-org rule rather than
        inventing a second one: a platform-endpoint delivery carries no
        ``account``, and an org sitting on the platform's own Stripe account is
        addressed without ``stripe_account`` — so both render as "no connected
        account" and must compare equal.

        Args:
            event: The inbound Stripe event.
            subscription: The row the event's metadata resolved to.

        Returns:
            ``True`` when the event may act on this row.
        """
        event_account = getattr(event, "account", "") or ""
        org_account = subscription.organization.stripe_account_id or ""
        if org_account == settings.STRIPE_ACCOUNT:
            org_account = ""
        if event_account == settings.STRIPE_ACCOUNT:
            event_account = ""
        return event_account == org_account

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

        subscription = (
            MembershipSubscription.objects.select_for_update(of=("self",))
            .select_related("organization")
            .filter(pk=subscription_pk)
            .first()
        )
        if subscription is None:
            # A paid session whose metadata matches nothing: Stripe created a
            # Subscription that will keep billing, and reconcile walks local
            # rows only, so it can never find this. Alert rather than shrug.
            stripe_incidents.record_subscription_checkout_without_row(
                session_id=str(session.get("id") or ""),
                membership_subscription_id=raw_id,
            )
            return

        # Connect authorization: ``membership_subscription_id`` is the only
        # attacker-choosable correlation key in the whole subscription pipeline
        # (everything downstream keys on Stripe-generated ``sub_``/``in_`` ids).
        # Every org holds a direct-charge Standard account and can mint a
        # Session with arbitrary metadata on it, so without this check an org
        # could link — and then drive — a subscription row belonging to another
        # org. Never raise: a 5xx here would wedge Stripe redelivery.
        if not self._event_account_owns(event, subscription):
            logger.warning(
                "subscription_checkout_completed_account_mismatch",
                session_id=session.get("id"),
                subscription_id=str(subscription.pk),
                event_account=getattr(event, "account", "") or "",
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

        No-op when *this invoice* is already recorded (the normal ordering), when
        the session carries no invoice reference, or when the invoice isn't paid
        yet (async payment methods — the later ``invoice.paid`` now resolves
        against the freshly-linked row). The guard is scoped to the invoice id,
        not to the subscription: a revival reuses the same row and keeps the
        member's whole payment history, so a subscription-wide ``exists()`` would
        disable this self-heal for exactly the flow that needs it. This mirrors
        the reconcile task's own ledger backfill.

        The ``Invoice.retrieve`` runs while holding this member's row lock — the
        documented single-member-blast-radius trade for webhook paths — and a
        Stripe failure propagates so the whole delivery (including the link and
        the dedup row) rolls back and Stripe redelivers.
        """
        from events.service import subscription_stripe_sync
        from events.service.subscription_stripe_payloads import _stripe_account_kwargs

        invoice_ref = session.get("invoice")
        invoice_id = invoice_ref.get("id") if isinstance(invoice_ref, dict) else invoice_ref
        if not invoice_id:
            return
        if MembershipPayment.objects.filter(stripe_invoice_id=invoice_id).exists():
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

    def handle_subscription_checkout_expired(self, event: stripe.Event) -> None:
        """Free the cap slot held by an abandoned ONLINE checkout.

        A PENDING row is created before the member is redirected to Stripe, so
        an abandoned session leaves a row that counts against the plan's
        ``max_subscriptions`` cap (and its ``sold_out`` flag) until the same
        user happens to retry. Stripe fires ``checkout.session.expired`` when
        the session's ``expires_at`` passes — clear the stranded row then.
        Sessions without our ``membership_subscription_id`` metadata (ticket
        checkouts, org-run sessions) are ignored; the nightly reconcile sweep
        backstops a dropped delivery of this event.
        """
        from events.service.subscription_stripe_service import _clear_stale_pending_checkout

        session = event.data.object
        raw_id = (session.get("metadata") or {}).get("membership_subscription_id")
        if not raw_id:
            return
        try:
            subscription_pk = uuid.UUID(raw_id)
        except ValueError:
            logger.warning(
                "subscription_checkout_expired_bad_metadata",
                session_id=session.get("id"),
                membership_subscription_id=raw_id,
            )
            return
        subscription = (
            MembershipSubscription.objects.select_for_update()
            .filter(
                pk=subscription_pk,
                status=MembershipSubscription.SubscriptionStatus.PENDING,
            )
            .first()
        )
        if subscription is None:
            return  # already completed, cleared, or superseded — nothing to free
        if subscription.stripe_subscription_id:
            return  # session completed after all (out-of-order delivery) — leave it
        if subscription.stripe_checkout_session_id != session.get("id"):
            return  # row already carries a newer session — expiring the old one frees nothing
        _clear_stale_pending_checkout(subscription)
        logger.info(
            "subscription_checkout_expired_cleared",
            session_id=session.get("id"),
            subscription_id=str(subscription_pk),
        )

    def _latest_refund_id(self, charge_data: t.Any) -> str:
        """Return the id of the charge's most recent refund, or ``""`` if it has none.

        Reading ``charge_data["refunds"]["data"]`` inline is not enough: at our
        pinned API version the ``charge.refunded`` payload doesn't embed the
        refunds list at all, so the audit field ended up empty on every
        production refund. :meth:`_resolve_refunds` (on the host handler) is the
        ticket side's answer to the same problem — it short-circuits on an
        embedded list and otherwise fetches outbound with the connected-account
        header. Stripe returns refunds newest-first in both cases, so the head
        is the refund this delivery is about.

        The fetch runs while holding this member's row lock — the documented
        single-member-blast-radius trade for the subscription webhook paths — and
        a Stripe failure propagates so Stripe redelivers the event.

        Args:
            charge_data: The Stripe ``charge.refunded`` ``data.object``.
        """
        refunds = self._resolve_refunds(charge_data)
        return t.cast(str, refunds[0].get("id") or "") if refunds else ""

    @staticmethod
    def _record_partial_refund(
        membership_payment: MembershipPayment,
        amount_refunded: int,
        refund_id: str,
    ) -> None:
        """Stamp the refund audit trail for a partially-refunded membership charge.

        Deliberately leaves ``status`` at SUCCEEDED: only a full refund revokes
        the period. Downstream fee invoicing and referral payouts are unchanged
        by design — netting a partial refund out of an already-issued platform
        fee invoice needs a credit note, which is a separate decision.

        Args:
            membership_payment: The matched ledger row.
            amount_refunded: Cumulative refunded amount, in minor units.
            refund_id: The Stripe refund id, from :meth:`_latest_refund_id`.
        """
        membership_payment.refund_amount = from_stripe_amount(amount_refunded, membership_payment.currency)
        membership_payment.refunded_at = timezone.now()
        if refund_id:
            membership_payment.stripe_refund_id = refund_id
        membership_payment.save(
            update_fields=["refund_amount", "refunded_at", "stripe_refund_id", "updated_at"],
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

        Idempotent on already-REFUNDED rows. ``charge.refunded`` fires for both
        partial and full refunds, but only a fully refunded charge flips the
        MembershipPayment to REFUNDED and triggers the auto-cancel path; a
        partial refund is recorded on the audit-trail fields and otherwise left
        alone (see :meth:`_record_partial_refund`).

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
        # Resolved once, before either branch writes: it may be an outbound call
        # (see _latest_refund_id), so a Stripe failure aborts the delivery before
        # the ledger is touched rather than between two writes.
        refund_id = self._latest_refund_id(charge_data)
        if amount is None or amount_refunded is None or amount_refunded < amount:
            # Partial refund: the member keeps the period they partly paid for,
            # so the row stays SUCCEEDED and the auto-cancel must not fire. It
            # still gets recorded — otherwise the refund leaves no trace at all
            # and our ledger silently disagrees with Stripe. ``amount_refunded``
            # is cumulative, so a later top-up refund that reaches the full
            # charge falls through to the branch below and flips to REFUNDED.
            self._record_partial_refund(membership_payment, amount_refunded, refund_id)
            logger.info(
                "stripe_subscription_partial_refund_recorded",
                payment_intent_id=charge_data.get("payment_intent"),
                membership_payment_id=str(membership_payment.id),
                amount=amount,
                amount_refunded=amount_refunded,
            )
            return

        payment = subscription_refunds.refund_payment(membership_payment, recorded_by=None)
        if refund_id:
            # Parity with the ticket side (_apply_refund_to_payment), which stamps
            # the refund id on every refund it applies. refund_payment is shared
            # with staff-recorded refunds, which have no Stripe refund to point at,
            # so the id is stamped here rather than inside it.
            payment.stripe_refund_id = refund_id
            payment.save(update_fields=["stripe_refund_id", "updated_at"])

        logger.info(
            "stripe_subscription_refund_processed",
            payment_intent_id=event.data.object.get("payment_intent"),
            membership_payment_id=str(membership_payment.id),
            subscription_id=str(membership_payment.subscription_id),
        )

    def handle_subscription_schedule_released(self, event: stripe.Event) -> None:
        """Clear the local pending-downgrade fields once Stripe releases a schedule.

        Two cases need this. First, ``release_online_schedule`` clears
        ``stripe_schedule_id``/``pending_plan`` locally *before* cancel/pause
        makes its own Stripe call; if that call raises, the enclosing atomic
        block rolls the clear back while the Stripe-side release stands — and no
        price swap follows, so ``_apply_stripe_price_swap`` never repairs it.
        The row would advertise a downgrade Stripe will never apply, and
        ``_validate_change_plan_state`` would block any further plan change.
        Second, an org can release a schedule straight from the Stripe Dashboard.

        Leaves ``plan`` alone: a schedule completing into a legitimately applied
        downgrade emits this alongside ``customer.subscription.updated``, and
        that is the path that owns the plan swap.
        """
        schedule = event.data.object
        schedule_id = schedule.get("id")
        if not schedule_id:
            return
        subscription = (
            MembershipSubscription.objects.select_for_update(of=("self",))
            .filter(stripe_schedule_id=schedule_id)
            .first()
        )
        if subscription is None:
            return
        update_fields = ["stripe_schedule_id", "updated_at"]
        subscription.stripe_schedule_id = ""
        if subscription.pending_plan_id:
            subscription.pending_plan = None
            update_fields.append("pending_plan")
        subscription.save(update_fields=update_fields)
        logger.info(
            "subscription_schedule_released_cleared",
            subscription_id=str(subscription.pk),
            stripe_schedule_id=schedule_id,
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
