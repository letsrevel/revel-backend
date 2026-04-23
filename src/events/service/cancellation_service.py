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
) -> t.Any:
    """End-to-end user-initiated cancellation. Implemented in Phase 5."""
    raise NotImplementedError
