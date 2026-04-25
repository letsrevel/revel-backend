"""User-initiated ticket cancellation & refund quoting.

Two pure functions (``quote_cancellation``, ``build_cancellation_preview``)
compute refund amounts from a ticket's snapshot; ``cancel_ticket_by_user``
orchestrates the end-to-end flow including Stripe refund and DB mutations.
"""

import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import structlog
from pydantic import ValidationError as PydanticValidationError

from events.models import Payment, Ticket, TicketTier
from events.models.ticket import CancellationBlockReason, CancellationSource
from events.utils.currency import to_stripe_amount
from events.utils.refund_policy import RefundPolicy, validate_refund_policy

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_CENT = Decimal("0.01")


class CancellationNotOwner(Exception):
    """Raised when the caller is not the ticket holder."""


class CancellationBlocked(Exception):
    """Raised when a business rule (ALREADY_CANCELLED, CHECKED_IN, ...) blocks cancellation.

    Attributes:
        reason: The specific ``CancellationBlockReason`` that prevented cancellation.
    """

    def __init__(self, reason: CancellationBlockReason) -> None:
        """Initialize with the blocking reason."""
        super().__init__(str(reason.label))
        self.reason = reason


class StripeRefundFailed(Exception):
    """Raised when a Stripe refund attempt fails after all internal retries.

    Attributes:
        detail: Human-readable description of the Stripe error.
    """

    def __init__(self, detail: str) -> None:
        """Initialize with the Stripe error detail string."""
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class CancellationResult:
    """Outcome of a successful ``cancel_ticket_by_user`` call.

    Attributes:
        ticket: The mutated ticket instance (status == CANCELLED).
        refund_amount: The refund issued (``Decimal("0")`` for free/offline tickets).
        currency: ISO-4217 currency code matching the refund.
        refund_status: ``Payment.RefundStatus`` value, or ``None`` for offline/free.
    """

    ticket: Ticket
    refund_amount: Decimal
    currency: str
    refund_status: str | None


@dataclass(frozen=True)
class RefundWindowDto:
    """One segment of the refund-vs-time curve for UI rendering."""

    refund_percentage: Decimal
    refund_amount: Decimal
    effective_until: datetime


@dataclass(frozen=True)
class RefundQuote:
    """Result of ``quote_cancellation``."""

    can_cancel: bool
    reason: CancellationBlockReason | None
    refund_amount: Decimal
    currency: str
    deadline: datetime | None


@dataclass(frozen=True)
class CancellationPreview:
    """Full preview payload for the preview endpoint."""

    can_cancel: bool
    reason: CancellationBlockReason | None
    refund_amount: Decimal
    currency: str
    deadline: datetime | None
    flat_fee: Decimal
    payment_method: str
    windows: list[RefundWindowDto] = field(default_factory=list)
    policy_snapshot: RefundPolicy | None = None


def _ticket_currency(ticket: Ticket) -> str:
    payment = getattr(ticket, "payment", None)
    if payment is not None:
        return str(payment.currency)
    return str(ticket.tier.currency)


def _ticket_amount(ticket: Ticket) -> Decimal:
    """Per-ticket refundable gross amount. Online → payment.amount; otherwise 0."""
    payment = getattr(ticket, "payment", None)
    if payment is None:
        return _ZERO
    if ticket.tier.payment_method != TicketTier.PaymentMethod.ONLINE:
        return _ZERO
    return Decimal(payment.amount)


def _deadline(ticket: Ticket) -> datetime:
    hours = ticket.tier.cancellation_deadline_hours
    if hours is None:
        return ticket.event.start
    return ticket.event.start - timedelta(hours=hours)


def _load_snapshot(ticket: Ticket) -> RefundPolicy | None:
    """Load the snapshot, tolerating malformed historical data.

    Malformed stored data falls back to "no refund" rather than 500ing the preview.
    Logged for ops visibility.
    """
    try:
        return validate_refund_policy(ticket.refund_policy_snapshot)
    except PydanticValidationError as exc:
        logger.warning(
            "refund_policy_snapshot_invalid",
            ticket_id=str(ticket.id),
            error=str(exc),
        )
        return None


def _block_reason(ticket: Ticket, now: datetime) -> CancellationBlockReason | None:
    if ticket.status == Ticket.TicketStatus.CANCELLED:
        return CancellationBlockReason.ALREADY_CANCELLED
    if ticket.status == Ticket.TicketStatus.CHECKED_IN:
        return CancellationBlockReason.CHECKED_IN
    if now >= ticket.event.start:
        return CancellationBlockReason.EVENT_STARTED
    if not ticket.tier.allow_user_cancellation:
        return CancellationBlockReason.NOT_PERMITTED
    if now > _deadline(ticket):
        return CancellationBlockReason.PAST_DEADLINE
    return None


def _refund_for_tier(gross: Decimal, tier_pct: Decimal, flat_fee: Decimal) -> Decimal:
    """Per-tier refund: ``gross × pct / 100 − flat_fee``, clamped to zero, quantized to cents.

    Single source of truth for the refund formula — used by both ``_compute_refund``
    (live quote against the user's current ``hours_remaining``) and ``_derive_windows``
    (preview of every window's refund amount). Keeps quote and preview from drifting.
    """
    adjusted = (gross * tier_pct) / Decimal(100) - flat_fee
    if adjusted <= _ZERO:
        return _ZERO
    return adjusted.quantize(_CENT, rounding=ROUND_HALF_UP)


def _compute_refund(policy: RefundPolicy, hours_remaining: Decimal, gross: Decimal) -> Decimal:
    for tier in policy.tiers:
        if hours_remaining >= tier.hours_before_event:
            return _refund_for_tier(gross, tier.refund_percentage, policy.flat_fee)
    return _ZERO


def quote_cancellation(ticket: Ticket, now: datetime) -> RefundQuote:
    """Compute whether ``ticket`` can be cancelled and the refund amount if so.

    Pure + stateless. Ownership (NOT_OWNER) is enforced by the controller, not here.

    Args:
        ticket: The ticket to evaluate.
        now: The current datetime (timezone-aware). Injected for testability.

    Returns:
        A ``RefundQuote`` describing whether cancellation is allowed and the
        computed refund amount.
    """
    reason = _block_reason(ticket, now)
    currency = _ticket_currency(ticket)
    deadline = _deadline(ticket)

    if reason is not None:
        return RefundQuote(
            can_cancel=False,
            reason=reason,
            refund_amount=_ZERO,
            currency=currency,
            deadline=deadline,
        )

    gross = _ticket_amount(ticket)
    policy = _load_snapshot(ticket)
    if policy is None or gross == _ZERO:
        return RefundQuote(
            can_cancel=True,
            reason=None,
            refund_amount=_ZERO,
            currency=currency,
            deadline=deadline,
        )

    # Convert timedelta → Decimal hours without going through float. ``total_seconds()``
    # returns a float, and ``Decimal(float)`` carries binary-float artifacts (e.g.
    # ``Decimal(0.1) → Decimal('0.10000000000000000555…')``) which can flip the
    # ``hours_remaining >= tier.hours_before_event`` comparison for users cancelling
    # exactly on a tier edge (T-48h ± a few µs). Sum the timedelta components instead.
    delta = ticket.event.start - now
    total_microseconds = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    hours_remaining = Decimal(total_microseconds) / Decimal(3_600_000_000)
    refund = _compute_refund(policy, hours_remaining, gross)
    return RefundQuote(
        can_cancel=True,
        reason=None,
        refund_amount=refund,
        currency=currency,
        deadline=deadline,
    )


def _derive_windows(
    policy: RefundPolicy,
    event_start: datetime,
    gross: Decimal,
) -> list[RefundWindowDto]:
    windows: list[RefundWindowDto] = []
    for tier in policy.tiers:
        amount = _refund_for_tier(gross, tier.refund_percentage, policy.flat_fee)
        windows.append(
            RefundWindowDto(
                refund_percentage=tier.refund_percentage,
                refund_amount=amount,
                effective_until=event_start - timedelta(hours=tier.hours_before_event),
            )
        )
    return windows


def build_cancellation_preview(ticket: Ticket, now: datetime) -> CancellationPreview:
    """Build the full preview DTO powering the UI timeline + decision state.

    Args:
        ticket: The ticket to preview cancellation for.
        now: The current datetime (timezone-aware). Injected for testability.

    Returns:
        A ``CancellationPreview`` with per-window breakdown and current quote.
    """
    quote = quote_cancellation(ticket, now)
    policy = _load_snapshot(ticket)
    gross = _ticket_amount(ticket)
    windows = _derive_windows(policy, ticket.event.start, gross) if policy is not None and gross > _ZERO else []
    flat_fee = policy.flat_fee if policy is not None else _ZERO
    return CancellationPreview(
        can_cancel=quote.can_cancel,
        reason=quote.reason,
        refund_amount=quote.refund_amount,
        currency=quote.currency,
        deadline=quote.deadline,
        flat_fee=flat_fee,
        payment_method=ticket.tier.payment_method,
        windows=windows,
        policy_snapshot=policy,
    )


def cancel_ticket_by_user(
    ticket: Ticket,
    user: t.Any,
    reason: str,
    now: datetime,
) -> CancellationResult:
    """Run the full user-initiated cancellation flow atomically.

    Validates ownership and business rules, optionally issues a Stripe refund,
    then mutates the ticket, payment, and tier rows within a single transaction.

    Args:
        ticket: The ticket to cancel. Must be fully loaded (tier, event, payment).
        user: The requesting user. Must match ``ticket.user``.
        reason: Free-text cancellation reason supplied by the user.
        now: Current datetime (timezone-aware). Injected for testability.

    Returns:
        A ``CancellationResult`` describing the outcome.

    Raises:
        CancellationNotOwner: caller is not the ticket holder.
        CancellationBlocked: business-rule guard failed.
        StripeRefundFailed: Stripe refund API call failed.

    Note:
        The Stripe ``Refund.create`` call happens **inside** the
        ``transaction.atomic()`` block. The trade-off is intentional:

        * **No double-charge.** ``idempotency_key=f"refund:{ticket.id}"``
          guarantees that a retry returns the same refund object, not a new
          one. So if the post-Stripe DB writes raise and the transaction
          rolls back, a user-driven retry produces the correct end state
          without charging twice.
        * **Self-healing via webhook.** Even without a retry, Stripe sends
          ``charge.refunded`` seconds later. ``handle_charge_refunded``
          matches the refund to the Payment via the ``ticket_id`` metadata
          (Branch 2) and reaches the same end state (ticket CANCELLED,
          payment REFUNDED, quantity_sold decremented) — but with degraded
          audit fields: ``cancellation_source`` becomes
          ``STRIPE_DASHBOARD`` instead of ``USER``, ``cancelled_by`` is
          ``NULL``, and ``cancellation_reason`` is empty. The financial
          state is correct; only the attribution is fuzzier.
        * **Lock contention is bounded.** The locked rows (this Ticket and
          its Payment) are user-scoped; concurrent cancels of *other*
          tickets on the same tier do not contend on these locks.

        The alternative — calling Stripe outside the transaction with a
        two-phase ``PENDING_CANCELLATION`` state and a janitor task — costs
        ~100 LoC of new control flow + a migration to recover audit-field
        accuracy on a single-digit-ppm failure path. Not worth it given
        webhook self-healing already covers the financial outcome.
    """
    from django.db import transaction
    from django.db.models import F

    if ticket.user_id != user.id:
        raise CancellationNotOwner()

    quote = quote_cancellation(ticket, now)
    if not quote.can_cancel:
        # reason is guaranteed non-None when can_cancel is False
        assert quote.reason is not None
        raise CancellationBlocked(quote.reason)

    refund_status: str | None = None

    with transaction.atomic():
        # Re-fetch the ticket under a row lock and re-check the two state-mutating
        # block reasons (ALREADY_CANCELLED, CHECKED_IN). Without this, two concurrent
        # cancel requests can both pass the pre-atomic quote, both issue Stripe
        # refunds (the idempotency key prevents a double charge but not a double DB
        # mutation), and both decrement quantity_sold. CHECKED_IN is racy because
        # check_in_ticket() does not take a row lock. The remaining block reasons
        # (EVENT_STARTED, NOT_PERMITTED, PAST_DEADLINE) depend only on the injected
        # ``now`` and on static tier/event config — they cannot flip inside this
        # transaction, so re-checking them would be dead work.
        locked_ticket = Ticket.objects.select_for_update().select_related("tier").get(pk=ticket.pk)
        if locked_ticket.status == Ticket.TicketStatus.CANCELLED:
            raise CancellationBlocked(CancellationBlockReason.ALREADY_CANCELLED)
        if locked_ticket.status == Ticket.TicketStatus.CHECKED_IN:
            raise CancellationBlocked(CancellationBlockReason.CHECKED_IN)

        # Online tickets with refund > 0 → hit Stripe before mutating local state.
        payment: Payment | None = Payment.objects.select_for_update().filter(ticket=locked_ticket).first()
        if (
            locked_ticket.tier.payment_method == TicketTier.PaymentMethod.ONLINE
            and payment is not None
            and payment.stripe_payment_intent_id
            and quote.refund_amount > _ZERO
        ):
            refund_status = _issue_stripe_refund(locked_ticket, payment, quote.refund_amount, quote.currency)

        # Set pre-save hints used by the TICKET_CANCELLED notification signal handler
        # so the user sees the refund amount in the same notification that announces
        # the cancellation (rather than only later via TICKET_REFUNDED on webhook).
        # Skip when there is no refund — templates gate on `{% if context.refund_amount %}`
        # and the truthy string "0.00" would render a misleading "Refund of 0..." line.
        if quote.refund_amount > _ZERO:
            locked_ticket._refund_amount = str(quote.refund_amount)  # type: ignore[attr-defined]
            locked_ticket._refund_currency = quote.currency  # type: ignore[attr-defined]

        locked_ticket.status = Ticket.TicketStatus.CANCELLED
        locked_ticket.cancelled_at = now
        locked_ticket.cancelled_by = user
        locked_ticket.cancellation_source = CancellationSource.USER
        locked_ticket.cancellation_reason = reason or ""
        locked_ticket.save(
            update_fields=[
                "status",
                "cancelled_at",
                "cancelled_by",
                "cancellation_source",
                "cancellation_reason",
            ]
        )
        ticket = locked_ticket

        TicketTier.objects.filter(pk=locked_ticket.tier_id, quantity_sold__gt=0).update(
            quantity_sold=F("quantity_sold") - 1
        )

    return CancellationResult(
        ticket=ticket,
        refund_amount=quote.refund_amount,
        currency=quote.currency,
        refund_status=refund_status,
    )


def _issue_stripe_refund(ticket: Ticket, payment: t.Any, amount: Decimal, currency: str) -> str:
    """Create a Stripe refund and mutate the Payment row. Returns refund_status.

    Hits the Stripe API synchronously. On any Stripe error the exception propagates
    so the enclosing ``transaction.atomic()`` rolls back and the ticket stays ACTIVE.

    Args:
        ticket: The ticket being cancelled (used for metadata and idempotency key).
        payment: The ``Payment`` instance associated with the ticket.
        amount: The refund amount in major currency units (e.g. ``Decimal("40.00")``).
        currency: ISO 4217 currency code, used to scale ``amount`` to Stripe's
            smallest-unit integer (matters for zero-decimal currencies like JPY).

    Returns:
        The string value of ``Payment.RefundStatus.PENDING``.

    Raises:
        StripeRefundFailed: on any Stripe error so the enclosing atomic() rolls back.
    """
    import stripe

    try:
        refund = stripe.Refund.create(
            payment_intent=payment.stripe_payment_intent_id,
            amount=to_stripe_amount(amount, currency),
            metadata={"ticket_id": str(ticket.id), "user_initiated": "true"},
            idempotency_key=f"refund:{ticket.id}",
        )
    except stripe.error.StripeError as exc:
        logger.error(
            "stripe_refund_failed",
            ticket_id=str(ticket.id),
            payment_id=str(payment.id),
            error=str(exc),
        )
        raise StripeRefundFailed(str(exc)) from exc

    payment.stripe_refund_id = refund.id
    payment.refund_amount = amount
    payment.refund_status = Payment.RefundStatus.PENDING
    payment.refund_failure_reason = ""
    payment.save(
        update_fields=[
            "stripe_refund_id",
            "refund_amount",
            "refund_status",
            "refund_failure_reason",
        ]
    )
    return str(Payment.RefundStatus.PENDING)
