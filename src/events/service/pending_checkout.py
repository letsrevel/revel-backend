"""Resume/cancel/cleanup for pending Stripe checkout batches.

Split out of ``stripe_service`` (1000-line file limit) and re-exported there,
so callers keep using ``stripe_service.resume_pending_checkout`` etc.
"""

from collections import Counter
from uuid import UUID

import stripe
import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from stripe.checkout import Session

from accounts.models import RevelUser
from events.models import Payment, Ticket, TicketTier
from events.service.waitlist_service import enqueue_waitlist_processing

logger = structlog.get_logger(__name__)

# Pin both credentials and API version at import time (mirrors stripe_service).
# This module makes its own outbound call (Session.retrieve in
# resume_pending_checkout), so it must not rely on another module's import
# side effects to set the pin.
stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION


def _release_batch_tier_capacity(ticket_ids: list[UUID]) -> None:
    """Decrement ``quantity_sold`` per tier for the given tickets (grouped per tier_id).

    A batch purchase puts every ticket on one tier, but a series-pass checkout spans
    one tier per covered event — decrementing a single tier by the whole batch count
    would over-release it (possibly below zero) and leak the others.

    The decrement is floored at zero (``Greatest``): if another route already
    released part of the batch, an unguarded decrement could cross zero and blow
    the ``PositiveIntegerField`` CHECK constraint (IntegrityError -> 500). A
    ``quantity_sold__gt=0`` filter wouldn't do — ``count`` can exceed 1.
    """
    tickets_per_tier: Counter[UUID] = Counter(
        Ticket.objects.filter(id__in=ticket_ids, tier_id__isnull=False).values_list("tier_id", flat=True)
    )
    for tier_id, count in tickets_per_tier.items():
        TicketTier.objects.filter(pk=tier_id).update(quantity_sold=Greatest(F("quantity_sold") - count, Value(0)))


def _batch_filter(payment: Payment) -> "Q":
    """Sibling-payment filter for a checkout batch.

    Reserved-but-not-sessioned batches share reservation_id (stripe_session_id is
    ""); once sessioned they also share the real stripe_session_id. Group by
    reservation_id when present, else fall back to the session id (legacy rows).
    """
    if payment.reservation_id is not None:
        return Q(reservation_id=payment.reservation_id)
    return Q(stripe_session_id=payment.stripe_session_id)


@transaction.atomic
def _cleanup_expired_batch(payment: Payment) -> None:
    """Clean up an expired payment batch (all sibling payments sharing payment's reservation/session)."""
    # Imported here to avoid a cycle (series_pass_service -> events.tasks -> services).
    from events.service.series_pass_service import expire_held_passes_for_tickets

    # PENDING only: another route (e.g. cancel_held_pass) may already have
    # cancelled part of the batch — its payments went FAILED and its tier
    # capacity was released there. Re-releasing (or hard-deleting the CANCELLED
    # audit tickets) here would double-decrement counters and destroy history.
    # Mirrors the cleanup_expired_payments beat task.
    batch_payments = Payment.objects.filter(_batch_filter(payment), status=Payment.PaymentStatus.PENDING)
    ticket_ids = list(batch_payments.values_list("ticket_id", flat=True))

    # Pass row before tier rows (SeriesPassPurchaseService.purchase's lock order).
    # Ticket-based (not session-based): a reserved-but-not-sessioned series pass's
    # held_pass.stripe_session_id is "" and would be missed by a session lookup (#632).
    expire_held_passes_for_tickets(ticket_ids)

    # Release capacity per tier before the rows disappear.
    _release_batch_tier_capacity(ticket_ids)

    # Clean up the pending tickets and payments in this batch. Non-PENDING
    # tickets (e.g. CANCELLED audit rows, past-event ACTIVE tickets) survive.
    batch_payments.delete()
    Ticket.objects.filter(id__in=ticket_ids, status=Ticket.TicketStatus.PENDING).delete()


def resume_pending_checkout(
    payment_id: str,
    user: RevelUser,
) -> str:
    """Resume a pending Stripe checkout session by payment ID.

    Retrieves the existing Stripe checkout URL for a pending payment.
    Cleans up expired sessions and all tickets in the same batch.

    Args:
        payment_id: The UUID of the pending payment.
        user: The user who initiated the purchase.

    Returns:
        The Stripe checkout URL.

    Raises:
        HttpError: 404 if payment not found, not owned by user, or session expired.
    """
    # Find the payment and verify ownership
    payment = (
        Payment.objects.filter(
            id=payment_id,
            user=user,
            status=Payment.PaymentStatus.PENDING,
        )
        .select_related("ticket__event__organization", "ticket__tier")
        .first()
    )

    if not payment:
        raise HttpError(404, str(_("No pending payment found.")))

    event = payment.ticket.event

    # Check if the payment has expired - cleanup commits in its own transaction
    if payment.has_expired():
        _cleanup_expired_batch(payment)
        raise HttpError(404, str(_("Checkout session has expired. Please start a new purchase.")))

    # Un-sessioned reservation (#632): reserve committed but the /checkout-session
    # step never ran (stripe_session_id==""). Create the Stripe session now — it is
    # idempotent (keyed on reservation_id) — so resume recovers the checkout instead
    # of a misleading "not found" 404.
    if not payment.stripe_session_id and payment.reservation_id is not None:
        # Lazy import: stripe_service imports this module (re-exports), so a top-level
        # import here would be circular.
        from events.service.stripe_service import create_batch_session, create_series_pass_session

        if payment.ticket.held_pass_id:
            return create_series_pass_session(reservation_id=payment.reservation_id)
        return create_batch_session(reservation_id=payment.reservation_id)

    # Retrieve and return the Stripe session URL
    try:
        session = Session.retrieve(
            payment.stripe_session_id,
            stripe_account=event.organization.stripe_account_id
            if event.organization.stripe_account_id != settings.STRIPE_ACCOUNT
            else None,
        )
        if session.url:
            return session.url
        raise HttpError(404, str(_("Checkout session is no longer valid.")))
    except stripe.error.InvalidRequestError:
        raise HttpError(404, str(_("Checkout session not found.")))


@transaction.atomic
def cancel_pending_checkout(
    payment_id: str,
    user: RevelUser,
) -> int:
    """Cancel a pending Stripe checkout and delete associated tickets.

    Deletes all payments and tickets in the same batch (same reservation_id,
    falling back to stripe_session_id for legacy rows).

    Args:
        payment_id: The UUID of the pending payment to cancel.
        user: The user who owns the payment.

    Returns:
        Number of tickets cancelled.

    Raises:
        HttpError: 404 if payment not found or not owned by user.
        HttpError: 400 if payment is not in PENDING status.
    """
    # Find the payment and verify ownership
    payment = (
        Payment.objects.filter(
            id=payment_id,
            user=user,
        )
        .select_related("ticket__tier")
        .first()
    )

    if not payment:
        raise HttpError(404, str(_("Payment not found.")))

    if payment.status != Payment.PaymentStatus.PENDING:
        raise HttpError(400, str(_("Only pending payments can be cancelled.")))

    # Imported here to avoid a cycle (series_pass_service -> events.tasks -> services).
    from events.service.series_pass_service import expire_held_passes_for_tickets

    # Find the still-pending sibling payments in this batch (same reservation_id,
    # falling back to session_id for legacy rows). PENDING only: another route
    # (e.g. cancel_held_pass) may already have cancelled part of the batch — its
    # payments went FAILED and its tier capacity was released there. Re-releasing
    # (or hard-deleting the CANCELLED audit tickets) here would double-decrement
    # counters and destroy history. Mirrors the cleanup_expired_payments beat task.
    batch_payments = Payment.objects.filter(_batch_filter(payment), status=Payment.PaymentStatus.PENDING)
    ticket_count = batch_payments.count()
    ticket_ids = list(batch_payments.values_list("ticket_id", flat=True))

    # Capture event ids before the ticket rows are deleted (a series-pass batch
    # spans multiple events; a plain batch has one).
    event_ids = set(Ticket.objects.filter(id__in=ticket_ids).values_list("event_id", flat=True))

    # Pass row before tier rows (SeriesPassPurchaseService.purchase's lock order).
    # Ticket-based (not session-based): a reserved-but-not-sessioned series pass's
    # held_pass.stripe_session_id is "" and would be missed by a session lookup (#632).
    expire_held_passes_for_tickets(ticket_ids)

    # Release capacity per tier before the rows disappear.
    _release_batch_tier_capacity(ticket_ids)

    # Delete the pending tickets and payments in this batch. Non-PENDING tickets
    # (e.g. CANCELLED audit rows, past-event ACTIVE tickets) survive.
    batch_payments.delete()
    Ticket.objects.filter(id__in=ticket_ids, status=Ticket.TicketStatus.PENDING).delete()

    for event_id in event_ids:
        enqueue_waitlist_processing(event_id)

    logger.info(
        "pending_checkout_cancelled",
        payment_id=payment_id,
        user_id=str(user.id),
        tickets_cancelled=ticket_count,
    )

    return ticket_count
