"""Organizer/system Stripe refunds — the single refund primitive.

Every path that moves refund money (user cancellation, series-pass cancel,
organizer API, bulk event cancellation) goes through ``issue_refund``. One
``Refund`` row per attempt; the ``charge.refunded`` webhook finalizes rows
(PENDING → SUCCEEDED) and maintains ``Payment``'s denormalized totals.
"""

import typing as t
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import structlog
from django.db.models import Q, Sum
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import (
    NothingToRefundError,
    RefundInsufficientBalanceError,
    StripeRefundFailed,
    TicketAlreadyCancelledError,
)
from events.models import Event, Payment, Refund, Ticket, TicketTier
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

    locked = Payment.objects.select_for_update().select_related("ticket__event__organization").get(pk=payment.pk)
    # Gate on the Payment row itself, not the ticket's tier: a series pass paid
    # online can be materialized onto an OFFLINE/FREE-tier ticket (the tier
    # governs display/checkout for that event, not how the pass itself was
    # charged), so `tier.payment_method` is not authoritative here — see
    # `series_pass_service.cancel_held_pass`.
    if not locked.stripe_payment_intent_id or locked.status not in (
        Payment.PaymentStatus.SUCCEEDED,
        Payment.PaymentStatus.REFUNDED,
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


def issue_refund_for_ticket(
    ticket: Ticket,
    *,
    amount: Decimal | None,
    initiated_by: RevelUser | None,
    reason: str,
    source: str,
    metadata_extra: dict[str, str] | None = None,
) -> Refund:
    """Resolve ``ticket``'s Payment and issue a refund inside one atomic transaction.

    Thin wrapper around :func:`issue_refund` for callers (the admin refund
    endpoint) that start from a ``Ticket`` rather than an already-resolved
    ``Payment``. Keeps two things consistent with every other refund path:

    - "no payment on this ticket" answers 409 ``NothingToRefundError`` like
      every other nothing-to-refund case, instead of a bare 404.
    - the Stripe call happens inside ``transaction.atomic()`` so a failure
      (e.g. ``balance_insufficient``) rolls back the PENDING ``Refund`` row
      instead of committing an orphan (ATOMIC_REQUESTS only rolls back on an
      *uncaught* exception escaping the view; Ninja Extra's own exception
      handling means a mapped exception like this one does not propagate that
      far, so the caller must open its own block).

    Raises:
        NothingToRefundError: no ``Payment`` row exists for this ticket.
        HttpError: 400 when an explicit ``amount`` is not in (0, remaining].
        RefundInsufficientBalanceError: Stripe declined for lack of funds.
        StripeRefundFailed: any other Stripe error.
    """
    from django.db import transaction

    payment = Payment.objects.filter(ticket=ticket).first()
    if payment is None:
        raise NothingToRefundError(str(_("This ticket has no payment to refund.")))
    with transaction.atomic():
        return issue_refund(
            payment,
            amount=amount,
            initiated_by=initiated_by,
            reason=reason,
            source=source,
            metadata_extra=metadata_extra,
        )


@dataclass(frozen=True)
class RefundContext:
    """Admin refund preview for one ticket."""

    payment_method: TicketTier.PaymentMethod
    amount_paid: Decimal
    currency: str
    total_refunded: Decimal
    total_pending: Decimal
    remaining_refundable: Decimal
    policy_suggested_amount: Decimal | None
    refunds: list[Refund]


def build_refund_context(ticket: Ticket, now: datetime) -> RefundContext:
    """Assemble the admin refund preview: paid/refunded/remaining + the policy-quoted suggestion.

    The policy suggestion reuses the user-cancellation quote (snapshot-driven); a
    block reason (NOT_PERMITTED, PAST_DEADLINE, ...) yields no suggestion rather
    than an error — the organizer may refund any amount regardless.
    """
    from events.service.cancellation_service import quote_cancellation

    payment = getattr(ticket, "payment", None)
    if payment is None or ticket.tier.payment_method != TicketTier.PaymentMethod.ONLINE:
        return RefundContext(
            payment_method=TicketTier.PaymentMethod(ticket.tier.payment_method),
            amount_paid=_ZERO,
            currency=str(ticket.tier.currency),
            total_refunded=_ZERO,
            total_pending=_ZERO,
            remaining_refundable=_ZERO,
            policy_suggested_amount=None,
            refunds=[],
        )
    rows = list(payment.refunds.all())
    succeeded = sum((r.amount for r in rows if r.status == Refund.RefundStatus.SUCCEEDED), _ZERO)
    pending = sum((r.amount for r in rows if r.status == Refund.RefundStatus.PENDING), _ZERO)
    quote = quote_cancellation(ticket, now)
    suggestion = quote.refund_amount if quote.can_cancel and quote.refund_amount > _ZERO else None
    return RefundContext(
        payment_method=TicketTier.PaymentMethod(ticket.tier.payment_method),
        amount_paid=Decimal(payment.amount),
        currency=str(payment.currency),
        total_refunded=succeeded,
        total_pending=pending,
        remaining_refundable=remaining_refundable(payment),
        policy_suggested_amount=suggestion,
        refunds=rows,
    )


@dataclass(frozen=True)
class EventRefundPreview:
    """Aggregates for the advisory bulk-refund preview."""

    active_tickets: int
    online_refundable_tickets: int
    offline_tickets: int
    currencies: list[dict[str, t.Any]]
    tickets_refund_started_at: datetime | None


def build_event_refund_preview(event: Event) -> EventRefundPreview:
    """Totals to refund per currency vs the connected account's available balance.

    The balance is advisory (racy by nature); a Stripe error fetching it yields
    ``available_balance=None`` rather than failing the preview.
    """
    import stripe
    from django.conf import settings

    tickets = Ticket.objects.filter(event=event).exclude(status=Ticket.TicketStatus.CANCELLED).select_related("tier")
    active = tickets.count()
    offline = tickets.exclude(tier__payment_method=TicketTier.PaymentMethod.ONLINE).count()
    payments = (
        Payment.objects.filter(
            ticket__event=event,
            status__in=[Payment.PaymentStatus.SUCCEEDED, Payment.PaymentStatus.REFUNDED],
            stripe_payment_intent_id__isnull=False,
            ticket__tier__payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        .exclude(ticket__status=Ticket.TicketStatus.CANCELLED)
        .prefetch_related("refunds")
    )
    totals: dict[str, Decimal] = {}
    refundable_tickets = 0
    for p in payments:
        remaining = remaining_refundable(p)
        if remaining > _ZERO:
            refundable_tickets += 1
            totals[p.currency] = totals.get(p.currency, _ZERO) + remaining

    balances: dict[str, Decimal | None] = dict.fromkeys(totals)
    org_account = event.organization.stripe_account_id
    if totals:
        try:
            kwargs: dict[str, t.Any] = {}
            if org_account and org_account != settings.STRIPE_ACCOUNT:
                kwargs["stripe_account"] = org_account
            balance = stripe.Balance.retrieve(**kwargs)  # type: ignore[no-untyped-call]
            from events.utils.currency import from_stripe_amount

            for entry in balance.available:
                # Stripe reports balance currencies lowercase; Payment.currency is
                # stored uppercase (settings.DEFAULT_CURRENCY / tier defaults) — normalize
                # before the dict lookup.
                cur = entry["currency"].upper()
                if cur in balances:
                    balances[cur] = from_stripe_amount(int(entry["amount"]), cur)
        except stripe.error.StripeError as exc:
            logger.warning("refund_preview_balance_failed", event_id=str(event.pk), error=str(exc))

    currencies: list[dict[str, t.Any]] = []
    for cur, total in sorted(totals.items()):
        available = balances[cur]
        currencies.append(
            {
                "currency": cur,
                "total_refundable": total,
                "available_balance": available,
                "balance_sufficient": (available >= total) if available is not None else None,
            }
        )
    return EventRefundPreview(
        active_tickets=active,
        online_refundable_tickets=refundable_tickets,
        offline_tickets=offline,
        currencies=currencies,
        tickets_refund_started_at=event.tickets_refund_started_at,
    )


def admin_cancel_ticket(
    ticket: Ticket,
    *,
    cancelled_by: RevelUser,
    reason: str | None,
    refund_amount: Decimal | None,
) -> Ticket:
    """Organizer ticket cancellation with an optional refund, any payment method.

    Offline/at-the-door tickets delegate to the existing offline primitives so
    their behavior (offline_refund_amount, record-only Payment mutation) is
    unchanged. Online tickets cancel + optionally refund via Stripe in one
    transaction — a Stripe failure rolls the cancellation back.
    """
    from django.db import transaction

    from events.service import ticket_service

    if ticket.tier.payment_method != TicketTier.PaymentMethod.ONLINE:
        if refund_amount is not None:
            return ticket_service.mark_offline_ticket_refunded(
                ticket, cancelled_by=cancelled_by, reason=reason, refund_amount=refund_amount
            )
        return ticket_service.cancel_offline_ticket(ticket, cancelled_by=cancelled_by, reason=reason)

    ticket_service._reject_series_pass_ticket(ticket)
    with transaction.atomic():
        locked_ticket = (
            Ticket.objects.select_for_update().select_related("tier", "event__organization").get(pk=ticket.pk)
        )
        if locked_ticket.status == Ticket.TicketStatus.CANCELLED:
            raise TicketAlreadyCancelledError
        if refund_amount is not None and refund_amount > _ZERO:
            payment = Payment.objects.select_for_update().filter(ticket=locked_ticket).first()
            if payment is None:
                raise NothingToRefundError(str(_("This ticket has no payment to refund.")))
            issue_refund(
                payment,
                amount=refund_amount,
                initiated_by=cancelled_by,
                reason=reason or "",
                source=Refund.Source.ORGANIZER_API,
            )
        ticket_service._cancel_offline_ticket_core(locked_ticket, cancelled_by=cancelled_by, reason=reason or "")
    return Ticket.objects.full().get(pk=locked_ticket.pk)
