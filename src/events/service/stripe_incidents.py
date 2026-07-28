"""Money-correctness incident signals for the Stripe checkout path (#750).

A reconciliation breach is the loudest money signal the ticketing system has, and
its evidence is perishable: ``events.cleanup_expired_payments`` sweeps every 5
minutes and would delete the PENDING ``Payment``/``Ticket`` rows of an unconfirmed
checkout, while the webhook's rollback means nothing about the failure can be
written to the database from the request itself. Whatever the operator will need
at 3am has to be emitted at the moment of detection, in full, or it is gone.

So each incident emits two halves, together, from one place:

* a **Prometheus counter** (``common.observability.metrics``) — the durable,
  actively-noticed half. One occurrence is the alert; labels stay
  low-cardinality on purpose.
* a **structured ERROR log** carrying every identifier needed to act without the
  database: the Stripe session and PaymentIntent to refund, the buyer, and the
  per-ticket breakdown to re-issue from.

For a session-total mismatch, the implicated rows themselves are additionally
placed under an **incident hold** (#756): ``record_session_total_mismatch``
dispatches ``events.hold_mismatch_payments`` with a bare ``.delay()`` — the
dispatch-then-raise exception in docs/engineering-notes.md, since the broker
message survives the webhook's deliberate rollback and the rows pre-exist the
request — which stamps ``Payment.incident_hold_at``. The expiry sweep retains
held rows so the operator finds real rows to reconcile, bounded in two ways:
clearing the field in the Payment admin resolves the incident (the next sweep
reclaims the rows normally), and an unresolved hold lapses after
``INCIDENT_HOLD_RETENTION`` (events/tasks/payments.py) so no row is immortal.
The log line stays self-contained regardless: in the rare race where the sweep
locked the rows before the hold landed, it remains the only record.
"""

import typing as t

import structlog

from common.observability.metrics import (
    STRIPE_SESSION_PAID_WITHOUT_PAYMENTS,
    STRIPE_SESSION_TOTAL_MISMATCH,
    SUBSCRIPTION_CHECKOUT_PAID_BUT_UNLINKED,
    SUBSCRIPTION_CHECKOUT_WHILE_TERMINAL,
    SUBSCRIPTION_CHECKOUT_WITHOUT_ROW,
    SUBSCRIPTION_PAID_WHILE_BLACKLISTED,
    SUBSCRIPTION_PAID_WHILE_PAUSED,
    SUBSCRIPTION_PAID_WHILE_TERMINAL,
    SUBSCRIPTION_PAYMENT_INTENT_UNRESOLVED,
)
from events.models import Payment

logger = structlog.get_logger(__name__)

CallSite = t.Literal["preflight", "webhook"]


class PaymentEvidence(t.TypedDict):
    """One row of the perishable breakdown, captured before the sweep can delete it."""

    payment_id: str
    ticket_id: str
    event_id: str
    tier_id: str | None
    guest_name: str
    amount: str


def _evidence(payments: list[Payment]) -> list[PaymentEvidence]:
    """Snapshot what re-issuing these tickets by hand would require."""
    return [
        PaymentEvidence(
            payment_id=str(p.id),
            ticket_id=str(p.ticket_id),
            event_id=str(p.ticket.event_id),
            tier_id=str(p.ticket.tier_id) if p.ticket.tier_id else None,
            guest_name=p.ticket.guest_name,
            amount=str(p.amount),
        )
        for p in payments
    ]


def record_session_total_mismatch(
    *,
    call_site: CallSite,
    payments: list[Payment],
    charged_minor_units: int,
    recorded_minor_units: int,
    currency: str,
    session_id: str | None = None,
    payment_intent_id: str | None = None,
) -> None:
    """Emit the counter and the self-contained ERROR line for a session-total breach.

    Called from both reconciliation points immediately before they raise, so the
    signal is emitted whether or not the surrounding transaction survives — the
    counter lives in process memory and the log line has already been handed to
    the logging queue by the time the rollback happens.

    Args:
        call_site: ``preflight`` (no session yet, nobody charged) or ``webhook``
            (the card has been charged).
        payments: The rows whose total disagrees with Stripe.
        charged_minor_units: What Stripe charged / is about to charge.
        recorded_minor_units: What our own books say.
        currency: Currency of both totals.
        session_id: The Stripe checkout session, when one exists.
        payment_intent_id: The PaymentIntent to refund, when one exists.
    """
    # Imported here to avoid a cycle (events.tasks -> services -> this module).
    from events.tasks.payments import hold_mismatch_payments

    STRIPE_SESSION_TOTAL_MISMATCH.labels(call_site=call_site).inc()
    if payments:
        # Bare .delay(), NOT on_commit: both call sites raise right after this
        # returns, rolling the request back — an on_commit callback would be
        # discarded and a synchronous UPDATE undone. The broker message is the
        # half that survives; the rows it targets pre-exist this request. See
        # "Dispatch-then-raise" in docs/engineering-notes.md (#756).
        hold_mismatch_payments.delay([str(p.id) for p in payments])
    buyer = payments[0].user if payments else None
    logger.error(
        "stripe_session_total_mismatch",
        call_site=call_site,
        session_id=session_id,
        payment_intent_id=payment_intent_id,
        charged_minor_units=charged_minor_units,
        recorded_minor_units=recorded_minor_units,
        currency=currency,
        payment_ids=[str(p.id) for p in payments],
        user_id=str(buyer.id) if buyer else None,
        user_email=buyer.email if buyer else None,
        reservation_id=str(payments[0].reservation_id) if payments and payments[0].reservation_id else None,
        payments=_evidence(payments),
    )


def record_paid_session_without_payments(
    *,
    session_id: str,
    amount_total: int,
    currency: str | None,
    payment_intent_id: str | None,
) -> None:
    """Emit the counter and ERROR line for a paid session we hold no Payment rows for.

    Money was captured against a session that has no record on our side, and the
    handler can only return 200 — a redelivery would find exactly the same
    nothing, so retrying cannot heal it. The alert is the only remaining thread:
    the Stripe session and PaymentIntent are enough to refund the buyer.

    Args:
        session_id: The Stripe checkout session.
        amount_total: What Stripe captured, in minor units (non-zero by caller contract).
        currency: Session currency, as reported by Stripe.
        payment_intent_id: The PaymentIntent to refund.
    """
    STRIPE_SESSION_PAID_WITHOUT_PAYMENTS.inc()
    logger.error(
        "stripe_session_paid_without_payments",
        session_id=session_id,
        payment_intent_id=payment_intent_id,
        charged_minor_units=amount_total,
        currency=currency,
    )


def record_subscription_paid_while_terminal(
    *,
    subscription_id: str,
    status: str,
    stripe_invoice_id: str,
    payment_intent_id: str,
    amount: str,
    currency: str,
) -> None:
    """Emit the counter and ERROR line for an invoice paid against a terminal row.

    The member was billed for a period they will never receive: the local row is
    CANCELLED/EXPIRED, so no membership is granted and the row is deliberately
    frozen against further mutation. Nothing downstream heals this — the nightly
    reconcile ignores terminal rows by design — so the refund has to be issued by
    hand, and the identifiers to do it are emitted here.

    Args:
        subscription_id: The local :class:`MembershipSubscription` pk.
        status: The terminal status the row was in when the invoice landed.
        stripe_invoice_id: The Stripe invoice that was paid.
        payment_intent_id: The PaymentIntent to refund (may be empty).
        amount: What changed hands, as a decimal string.
        currency: Payment currency.
    """
    SUBSCRIPTION_PAID_WHILE_TERMINAL.inc()
    logger.error(
        "subscription_paid_while_terminal",
        subscription_id=subscription_id,
        subscription_status=status,
        stripe_invoice_id=stripe_invoice_id,
        payment_intent_id=payment_intent_id,
        amount=amount,
        currency=currency,
    )


def record_subscription_paid_while_paused(
    *,
    subscription_id: str,
    organization_id: str,
    user_id: str,
    stripe_invoice_id: str,
    payment_intent_id: str,
    amount: str,
    currency: str,
) -> None:
    """Emit the counter and ERROR line for an invoice paid against a PAUSED row.

    Staff paused the subscription, but an invoice that was already open when the
    pause landed still settled (a Smart Retry, or the member paying from the
    hosted invoice page). The payment is recorded and the period advances, yet
    the row deliberately stays PAUSED — Stripe still reports ``pause_collection``
    and staff intent wins — so the member is not resumed and no member-facing
    notification fires (the dispatch gates only speak for ACTIVE/PAST_DUE/PENDING
    transitions). Nothing downstream reconciles that, so ops has to decide
    between resuming the membership and refunding the invoice.

    Args:
        subscription_id: The local :class:`MembershipSubscription` pk.
        organization_id: The organization the paused subscription belongs to.
        user_id: The paying member (id only — no email in incident logs).
        stripe_invoice_id: The Stripe invoice that was paid.
        payment_intent_id: The PaymentIntent to refund (may be empty).
        amount: What changed hands, as a decimal string.
        currency: Payment currency.
    """
    SUBSCRIPTION_PAID_WHILE_PAUSED.inc()
    logger.error(
        "subscription_paid_while_paused",
        subscription_id=subscription_id,
        organization_id=organization_id,
        user_id=user_id,
        stripe_invoice_id=stripe_invoice_id,
        payment_intent_id=payment_intent_id,
        amount=amount,
        currency=currency,
    )


def record_subscription_payment_intent_unresolved(
    *,
    subscription_id: str,
    stripe_invoice_id: str,
) -> None:
    """Emit the counter and ERROR line for a ledger row with no PaymentIntent id.

    ``charge.refunded`` matches membership payments solely on
    ``stripe_payment_intent_id``, and the org-admin refund endpoint refuses ONLINE
    payments outright — so a row stored without an intent id can never be marked
    REFUNDED by any path. The invoice id is enough for an operator to re-resolve
    the intent from the Stripe dashboard and repair the row.

    Args:
        subscription_id: The local :class:`MembershipSubscription` pk.
        stripe_invoice_id: The invoice whose PaymentIntent could not be resolved.
    """
    SUBSCRIPTION_PAYMENT_INTENT_UNRESOLVED.inc()
    logger.error(
        "subscription_payment_intent_unresolved",
        subscription_id=subscription_id,
        stripe_invoice_id=stripe_invoice_id,
    )


def record_subscription_paid_while_blacklisted(
    *,
    subscription_id: str,
    organization_id: str,
    user_id: str,
    user_email: str,
    stripe_subscription_id: str,
) -> None:
    """Emit the counter and ERROR line for an invoice paid by a hard-blacklisted user.

    The member was banned (or their entry hard-matched) while a renewal was in
    flight, so their ``OrganizationMember`` row is gone and re-creating it would
    silently un-ban them. We keep the payment (the money moved) but grant no
    membership, so the row is owed a manual refund/cancel that nothing downstream
    issues — the identifiers to do it by hand are emitted here. With ban/removal
    now cancelling the subscription up front, this is only reachable as a rare
    race (payment landing between the ban and the best-effort Stripe cancel).

    Args:
        subscription_id: The local :class:`MembershipSubscription` pk.
        organization_id: The organization the user is blacklisted in.
        user_id: The blacklisted user.
        user_email: The blacklisted user's email (to locate them in Stripe).
        stripe_subscription_id: The Stripe subscription still billing them.
    """
    SUBSCRIPTION_PAID_WHILE_BLACKLISTED.inc()
    logger.error(
        "subscription_paid_while_blacklisted",
        subscription_id=subscription_id,
        organization_id=organization_id,
        user_id=user_id,
        user_email=user_email,
        stripe_subscription_id=stripe_subscription_id,
    )


def record_subscription_checkout_without_row(
    *,
    session_id: str,
    membership_subscription_id: str,
) -> None:
    """Emit the counter and ERROR line for a paid subscription session with no row.

    The session completed on Stripe — a Subscription exists and will keep billing
    — but its metadata points at no local row, so no membership is granted and no
    ledger entry is written. The reconcile sweep walks local rows only and can
    never discover this, and the webhook returns 200 so Stripe will not retry.

    Args:
        session_id: The completed Stripe Checkout Session.
        membership_subscription_id: The unmatched id carried in session metadata.
    """
    SUBSCRIPTION_CHECKOUT_WITHOUT_ROW.inc()
    logger.error(
        "subscription_checkout_without_row",
        session_id=session_id,
        membership_subscription_id=membership_subscription_id,
    )


def record_subscription_checkout_while_terminal(
    *,
    subscription_id: str,
    status: str,
    session_id: str,
    stripe_subscription_id: str,
) -> None:
    """Emit the counter and ERROR line for a checkout completed against a terminal row.

    The member paid a Checkout Session that was still live when the local row
    went terminal (immediate cancel, ban, refund auto-cancel). The handler
    refuses to link the resulting Stripe Subscription — a terminal row stays
    frozen, and linking would grant a membership nobody is entitled to — and
    instead cancels it on Stripe after commit. The Stripe side therefore stops
    billing, but the first invoice has already been charged and no local ledger
    row exists for it, so the refund has to be issued by hand.

    Args:
        subscription_id: The local :class:`MembershipSubscription` pk.
        status: The terminal status the row was in when the session completed.
        session_id: The completed Stripe Checkout Session.
        stripe_subscription_id: The Stripe Subscription the session minted.
    """
    SUBSCRIPTION_CHECKOUT_WHILE_TERMINAL.inc()
    logger.error(
        "subscription_checkout_while_terminal",
        subscription_id=subscription_id,
        subscription_status=status,
        session_id=session_id,
        stripe_subscription_id=stripe_subscription_id,
    )


def record_subscription_checkout_paid_but_unlinked(
    *,
    subscription_id: str,
    organization_id: str,
    user_id: str,
    session_id: str,
) -> None:
    """Emit the counter and ERROR line for a paid checkout that never linked its row.

    The nightly stale-PENDING sweep found a row untouched for a day whose Checkout
    Session Stripe reports as ``complete``: the member was charged and a Stripe
    Subscription exists, but ``checkout.session.completed`` never landed the link
    (redeliveries exhausted, or each one rolled back). Nothing self-heals — the
    reconcile pass keys on ``stripe_subscription_id``, which is still empty — so
    the sweep deliberately leaves the row in place as the only handle back to the
    money, and a human has to link the Stripe Subscription or refund the session.

    Args:
        subscription_id: The local :class:`MembershipSubscription` pk.
        organization_id: The organization the subscription belongs to.
        user_id: The paying member (id only — no email in incident logs).
        session_id: The completed Stripe Checkout Session.
    """
    SUBSCRIPTION_CHECKOUT_PAID_BUT_UNLINKED.inc()
    logger.error(
        "subscription_checkout_paid_but_unlinked",
        subscription_id=subscription_id,
        organization_id=organization_id,
        user_id=user_id,
        session_id=session_id,
    )
