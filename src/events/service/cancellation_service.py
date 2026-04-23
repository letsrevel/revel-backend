"""User-initiated ticket cancellation & refund quoting.

Two pure functions (``quote_cancellation``, ``build_cancellation_preview``)
compute refund amounts from a ticket's snapshot; ``cancel_ticket_by_user``
orchestrates the end-to-end flow including Stripe refund and DB mutations.
"""

import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import structlog
from pydantic import ValidationError as PydanticValidationError

from events.models import Ticket, TicketTier
from events.models.ticket import CancellationBlockReason
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


def _compute_refund(policy: RefundPolicy, hours_remaining: Decimal, gross: Decimal) -> Decimal:
    for tier in policy.tiers:
        if hours_remaining >= tier.hours_before_event:
            base = (gross * tier.refund_percentage) / Decimal(100)
            adjusted = base - policy.flat_fee
            if adjusted <= _ZERO:
                return _ZERO
            return adjusted.quantize(_CENT, rounding=ROUND_HALF_EVEN)
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

    hours_remaining = Decimal((ticket.event.start - now).total_seconds()) / Decimal(3600)
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
        base = (gross * tier.refund_percentage) / Decimal(100)
        adjusted = base - policy.flat_fee
        amount = _ZERO if adjusted <= _ZERO else adjusted.quantize(_CENT, rounding=ROUND_HALF_EVEN)
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
    """
    from django.db import transaction
    from django.db.models import F

    from events.models import Payment
    from events.models import TicketTier as _TicketTier  # local import, avoid circular

    if ticket.user_id != user.id:
        raise CancellationNotOwner()

    quote = quote_cancellation(ticket, now)
    if not quote.can_cancel:
        # reason is guaranteed non-None when can_cancel is False
        assert quote.reason is not None
        raise CancellationBlocked(quote.reason)

    refund_status: str | None = None

    with transaction.atomic():
        # Lock the tier row to serialize inventory updates with concurrent purchases.
        _TicketTier.objects.select_for_update().filter(pk=ticket.tier_id).first()

        # Online tickets with refund > 0 → hit Stripe before mutating local state.
        payment: Payment | None = Payment.objects.filter(ticket=ticket).first()
        if (
            ticket.tier.payment_method == _TicketTier.PaymentMethod.ONLINE
            and payment is not None
            and payment.stripe_payment_intent_id
            and quote.refund_amount > _ZERO
        ):
            refund_status = _issue_stripe_refund(ticket, payment, quote.refund_amount)

        # Set pre-save hints used by the existing notification signal handlers.
        ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
        ticket._refund_amount = f"{quote.refund_amount} {quote.currency}"  # type: ignore[attr-defined]

        ticket.status = Ticket.TicketStatus.CANCELLED
        ticket.cancelled_at = now
        ticket.cancelled_by = user
        ticket.cancellation_source = "user"
        ticket.cancellation_reason = reason or ""
        ticket.save(
            update_fields=[
                "status",
                "cancelled_at",
                "cancelled_by",
                "cancellation_source",
                "cancellation_reason",
            ]
        )

        _TicketTier.objects.filter(pk=ticket.tier_id, quantity_sold__gt=0).update(quantity_sold=F("quantity_sold") - 1)

    return CancellationResult(
        ticket=ticket,
        refund_amount=quote.refund_amount,
        currency=quote.currency,
        refund_status=refund_status,
    )


def _issue_stripe_refund(ticket: Ticket, payment: t.Any, amount: Decimal) -> str:
    """Create a Stripe refund and mutate the Payment row. Returns refund_status.

    Hits the Stripe API synchronously. On any Stripe error the exception propagates
    so the enclosing ``transaction.atomic()`` rolls back and the ticket stays ACTIVE.

    Args:
        ticket: The ticket being cancelled (used for metadata and idempotency key).
        payment: The ``Payment`` instance associated with the ticket.
        amount: The refund amount in major currency units (e.g. ``Decimal("40.00")``).

    Returns:
        The string value of ``Payment.RefundStatus.PENDING``.

    Raises:
        StripeRefundFailed: on any Stripe error so the enclosing atomic() rolls back.
    """
    import stripe

    from events.models import Payment

    try:
        refund = stripe.Refund.create(
            payment_intent=payment.stripe_payment_intent_id,
            amount=int(amount * 100),
            metadata={"ticket_id": str(ticket.id), "user_initiated": "true"},
            idempotency_key=f"refund:{ticket.id}",
        )
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
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
