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

from common.observability.metrics import STRIPE_SESSION_PAID_WITHOUT_PAYMENTS, STRIPE_SESSION_TOTAL_MISMATCH
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
