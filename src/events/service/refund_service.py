"""Organizer/system Stripe refunds — the single refund primitive.

Every path that moves refund money (user cancellation, series-pass cancel,
organizer API, bulk event cancellation) goes through ``issue_refund``. One
``Refund`` row per attempt; the ``charge.refunded`` webhook finalizes rows
(PENDING → SUCCEEDED) and maintains ``Payment``'s denormalized totals.
"""

import typing as t
from decimal import ROUND_HALF_UP, Decimal

import structlog
from django.db.models import Q, Sum
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import NothingToRefundError, RefundInsufficientBalanceError, StripeRefundFailed
from events.models import Payment, Refund, TicketTier
from events.utils.currency import to_stripe_amount

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_CENT = Decimal("0.01")


def remaining_refundable(payment: Payment) -> Decimal:
    """The amount still refundable: ``payment.amount − Σ non-FAILED refund rows`` (floor 0).

    PENDING rows count against the remainder so a second refund cannot
    over-refund while the first one's webhook is still in flight.
    """
    taken = (
        payment.refunds.filter(~Q(status=Refund.RefundStatus.FAILED)).aggregate(total=Sum("amount"))["total"] or _ZERO
    )
    remaining = Decimal(payment.amount) - taken
    return remaining if remaining > _ZERO else _ZERO


def issue_refund(
    payment: Payment,
    *,
    amount: Decimal | None,
    initiated_by: RevelUser | None,
    reason: str,
    source: str,
    metadata_extra: dict[str, str] | None = None,
) -> Refund:
    """Create a ``Refund`` row and the matching Stripe refund. Caller must be inside ``atomic()``.

    The Stripe call happens inside the caller's transaction on purpose — same
    documented trade-off as ``cancellation_service.cancel_ticket_by_user``: the
    idempotency key ``refund:{payment.pk}:{sequence}:{amount}`` is deterministic
    for a retry after rollback (same sequence, same amount → Stripe returns the
    same refund object), and the ``charge.refunded`` webhook self-heals a lost
    response via the ``refund_id`` metadata.

    Args:
        payment: The Payment to refund (will be re-locked via select_for_update).
        amount: Refund amount in major units; ``None`` = full remaining refundable.
        initiated_by: The acting user, if any.
        reason: Free-text reason stored on the row.
        source: A ``Refund.Source`` value.
        metadata_extra: Extra Stripe metadata entries (merged over the defaults).

    Returns:
        The PENDING ``Refund`` row (webhook flips it to SUCCEEDED).

    Raises:
        NothingToRefundError: not an online Stripe payment, or nothing left to refund.
        HttpError: 400 when an explicit ``amount`` is not in (0, remaining].
        RefundInsufficientBalanceError: Stripe declined for lack of funds.
        StripeRefundFailed: any other Stripe error.
    """
    import stripe
    from django.conf import settings

    locked = (
        Payment.objects.select_for_update()
        .select_related("ticket__tier", "ticket__event__organization")
        .get(pk=payment.pk)
    )
    is_online = locked.ticket.tier.payment_method == TicketTier.PaymentMethod.ONLINE
    if (
        not is_online
        or not locked.stripe_payment_intent_id
        or locked.status not in (Payment.PaymentStatus.SUCCEEDED, Payment.PaymentStatus.REFUNDED)
    ):
        raise NothingToRefundError(str(_("This payment has no refundable Stripe charge.")))

    remaining = remaining_refundable(locked)
    if remaining <= _ZERO:
        raise NothingToRefundError(str(_("This payment is already fully refunded.")))

    resolved = (amount if amount is not None else remaining).quantize(_CENT, rounding=ROUND_HALF_UP)
    if resolved <= _ZERO or resolved > remaining:
        raise HttpError(
            400,
            str(_("Refund amount must be between 0 and the remaining refundable amount (%(amount)s)."))
            % {"amount": remaining},
        )

    sequence = locked.refunds.filter(~Q(status=Refund.RefundStatus.FAILED)).count()
    refund_row = Refund.objects.create(
        payment=locked,
        amount=resolved,
        currency=locked.currency,
        status=Refund.RefundStatus.PENDING,
        initiated_by=initiated_by,
        reason=reason or "",
        source=source,
    )

    metadata = {"ticket_id": str(locked.ticket_id), "refund_id": str(refund_row.pk), "source": source}
    if metadata_extra:
        metadata.update(metadata_extra)
    refund_kwargs: dict[str, t.Any] = {
        "payment_intent": locked.stripe_payment_intent_id,
        "amount": to_stripe_amount(resolved, locked.currency),
        "metadata": metadata,
        "idempotency_key": f"refund:{locked.pk}:{sequence}:{resolved}",
    }
    org_stripe_account = locked.ticket.event.organization.stripe_account_id
    if org_stripe_account and org_stripe_account != settings.STRIPE_ACCOUNT:
        refund_kwargs["stripe_account"] = org_stripe_account

    try:
        stripe_refund = stripe.Refund.create(**refund_kwargs)
    except stripe.error.StripeError as exc:
        logger.error(
            "stripe_refund_failed",
            payment_id=str(locked.pk),
            refund_row_id=str(refund_row.pk),
            code=getattr(exc, "code", None),
            error=str(exc),
        )
        if getattr(exc, "code", None) == "balance_insufficient":
            raise RefundInsufficientBalanceError() from exc
        raise StripeRefundFailed(str(exc)) from exc

    refund_row.stripe_refund_id = stripe_refund.id
    refund_row.save(update_fields=["stripe_refund_id"])
    # Mirror the latest attempt on the legacy Payment fields (webhook writes the totals).
    locked.stripe_refund_id = stripe_refund.id
    locked.refund_status = Payment.RefundStatus.PENDING
    locked.refund_failure_reason = ""
    locked.save(update_fields=["stripe_refund_id", "refund_status", "refund_failure_reason"])
    return refund_row
