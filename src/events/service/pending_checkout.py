"""Resume/cancel/cleanup for pending Stripe checkout batches.

Split out of ``stripe_service`` (1000-line file limit) and re-exported there,
so callers keep using ``stripe_service.resume_pending_checkout`` etc.
"""

import functools
import typing as t
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from uuid import UUID

import stripe
import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q, QuerySet, Value
from django.db.models.functions import Greatest
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from stripe.checkout import Session

from accounts.models import RevelUser
from common.service.stripe_config import configure_stripe
from events.models import EventSeries, Organization, Payment, SeriesPass, Ticket, TicketTier
from events.service import discount_code_service
from events.service.waitlist_service import enqueue_waitlist_processing

logger = structlog.get_logger(__name__)

configure_stripe()


def _release_batch_tier_capacity(ticket_ids: list[UUID]) -> None:
    """Decrement ``quantity_sold`` per tier for the given tickets (grouped per tier_id).

    A batch purchase can span several tiers (a multi-tier cart), and a series-pass
    checkout spans one tier per covered event — decrementing a single tier by the
    whole batch count would over-release it (possibly below zero) and leak the others.

    The decrement is floored at zero (``Greatest``): if another route already
    released part of the batch, an unguarded decrement could cross zero and blow
    the ``PositiveIntegerField`` CHECK constraint (IntegrityError -> 500). A
    ``quantity_sold__gt=0`` filter wouldn't do — ``count`` can exceed 1.

    ``status=PENDING`` filter: a ticket can be CANCELLED while its Payment is still
    PENDING (cancel_ticket_by_user -> _finalize_cancellation releases the tier slot
    and flips the ticket to CANCELLED, but never touches the Payment). Counting that
    ticket here would release its slot a second time. Mirrors the beat task's Counter
    guard (cleanup_expired_payments) and the PENDING-only ticket deletion already done
    by both callers of this function (#632).
    """
    tickets_per_tier: Counter[UUID] = Counter(
        Ticket.objects.filter(id__in=ticket_ids, tier_id__isnull=False, status=Ticket.TicketStatus.PENDING).values_list(
            "tier_id", flat=True
        )
    )
    # Sorted for a deterministic tier lock order across concurrent releases.
    for tier_id, count in sorted(tickets_per_tier.items()):
        TicketTier.objects.filter(pk=tier_id).update(quantity_sold=Greatest(F("quantity_sold") - count, Value(0)))


def _release_batch_discount_uses(ticket_ids: list[UUID]) -> None:
    """Give back the discount code uses of the batch's still-PENDING tickets.

    Same PENDING filter as ``_release_batch_tier_capacity`` (a user-cancelled ticket already
    released its use), and called right after it, so ``DiscountCode`` rows are locked after the
    tier rows (see ``discount_code_service.release_discount_uses``). Callers have already
    locked the batch's Payment rows, and this must run before the tickets are deleted.
    """
    discount_code_service.release_discount_uses(
        Ticket.objects.filter(
            id__in=ticket_ids, discount_code_id__isnull=False, status=Ticket.TicketStatus.PENDING
        ).values_list("discount_code_id", flat=True)
    )


# In-flight hold extension for the session-create step (#632): claimed before the
# lock-free Stripe round-trip so the cleanup_expired_payments beat task can't
# reclaim the reservation mid-call. Only needs to outlive one Stripe call; the
# successful stamp replaces it with PAYMENT_DEFAULT_EXPIRY_MINUTES.
_IN_FLIGHT_CLAIM_GRACE = timedelta(minutes=5)


def reservation_owned_by(reservation_id: UUID, user: RevelUser | None) -> bool:
    """Whether the reservation has Payment rows accessible to the caller (#632).

    Ownership gate shared by the checkout-session endpoints. ``user=None`` is the
    unauthenticated guest route: its bearer ``reservation_id`` must only unlock
    guest-originated reservations, so an authenticated user's reservation is not
    redeemable there (its own endpoint enforces ownership).
    """
    owner = Q(user=user) if user is not None else Q(user__guest=True)
    return Payment.objects.filter(owner, reservation_id=reservation_id).exists()


def claim_reservation_hold(reservation_id: UUID) -> None:
    """Atomically extend a reservation's hold before the lock-free Stripe call (#632).

    Replaces a read-only has_expired() check in the session-create step: bumping
    expires_at under the same conditions the expiry sweep filters on means the
    sweep cannot reclaim the rows while Session.create is in flight. Zero rows
    claimed == the reservation already expired (or was concurrently reclaimed).
    The bump rolls back with the caller's transaction if the Stripe call fails.

    Extend-only (Greatest): a double-submit's claim lands on rows the winner already
    stamped with the 45-minute payment window — flattening them back to the 5-minute
    grace would let the sweep reclaim rows whose Stripe session is still payable.

    Raises:
        HttpError: 404 if no still-valid PENDING rows exist for the reservation.
    """
    claimed = Payment.objects.filter(
        reservation_id=reservation_id,
        status=Payment.PaymentStatus.PENDING,
        expires_at__gt=timezone.now(),
    ).update(expires_at=Greatest(F("expires_at"), timezone.now() + _IN_FLIGHT_CLAIM_GRACE))
    if claimed == 0:
        raise HttpError(404, str(_("Reservation has expired. Please start a new purchase.")))


def stamp_session_or_expire(
    reservation_id: UUID,
    session: Session,
    *,
    expected: int,
    expires_at: datetime,
    stripe_account_id: str | None,
    log_event: str,
) -> None:
    """Stamp a just-created Stripe session onto the reservation's PENDING rows (#632).

    A zero-row stamp means the reservation was reclaimed (user cancel / expiry
    sweep) while Session.create was in flight: best-effort expire the session and
    404 WITHOUT releasing the URL — an unreleased session is unpayable even if
    the expire fails, so a paid session with no reconcilable Payment rows stays
    unreachable.

    Ticket must still be PENDING too: a user-cancelled ticket's orphaned PENDING
    Payment (see _release_batch_tier_capacity) is excluded from the session's line
    items, so stamping it would let the webhook mark it SUCCEEDED for money that
    was never charged. Left un-stamped, the expiry sweep reclaims it.

    Raises:
        HttpError: 404 if the reservation vanished mid-flight.
    """
    stamped = Payment.objects.filter(
        reservation_id=reservation_id,
        status=Payment.PaymentStatus.PENDING,
        ticket__status=Ticket.TicketStatus.PENDING,
    ).update(
        stripe_session_id=session.id,
        expires_at=expires_at,
    )
    if stamped == 0:
        expire_stripe_sessions_best_effort([session.id], stripe_account_id)
        raise HttpError(404, str(_("Reservation has expired. Please start a new purchase.")))
    if stamped != expected:
        logger.warning(log_event, reservation_id=str(reservation_id), stamped=stamped, expected=expected)


def expire_stripe_sessions_best_effort(session_ids: list[str], stripe_account_id: str | None) -> None:
    """Best-effort expiry of Stripe Checkout sessions whose local rows are gone (#632).

    A session left payable after its Payment rows were reclaimed is money the
    webhook can never reconcile (handle_checkout_session_completed finds no rows
    and returns). Failures are logged, not raised: callers either never released
    the URL (session-create stamp miss) or run post-commit (cancel), so an
    un-expired session is a bounded risk, not a correctness dependency.
    """
    expire_kwargs: dict[str, t.Any] = {}
    if stripe_account_id and stripe_account_id != settings.STRIPE_ACCOUNT:
        expire_kwargs["stripe_account"] = stripe_account_id
    for session_id in session_ids:
        try:
            Session.expire(session_id, **expire_kwargs)
        except stripe.error.StripeError as exc:
            logger.warning("pending_checkout_session_expire_failed", session_id=session_id, error=str(exc))


def _lock_unfinished_sessions(tickets: QuerySet[Ticket]) -> dict[str | None, set[str]]:
    """Lock all Payment rows of ``tickets`` (pk order) and return the open sessions of the unfinished ones.

    Unfinished means PENDING, or FAILED with a session that has not expired yet: the event-cancel sweep marks
    a pending checkout FAILED and leaves its session open (a late payment is then refunded, but only while the
    row exists). The statuses are read under the lock.
    """
    sessions_by_account: dict[str | None, set[str]] = defaultdict(set)
    locked = (
        Payment.objects.select_for_update(of=("self",))
        .filter(ticket__in=tickets)
        .order_by("pk")
        .values_list("status", "expires_at", "stripe_session_id", "ticket__event__organization__stripe_account_id")
    )
    now = timezone.now()
    for status, expires_at, session_id, account_id in locked:
        open_failed = status == Payment.PaymentStatus.FAILED and expires_at > now
        if session_id and (status == Payment.PaymentStatus.PENDING or open_failed):
            sessions_by_account[account_id].add(session_id)
    return sessions_by_account


def _lock_rows(rows: QuerySet[t.Any], *, no_key: bool = False) -> None:
    """Lock ``rows`` (``FOR UPDATE``, or ``FOR NO KEY UPDATE``; only the rows of the model itself) in pk order."""
    list(rows.select_for_update(no_key=no_key, of=("self",)).order_by("pk").values_list("pk", flat=True))


def _lock_delete_scope(
    passes: QuerySet[SeriesPass] | None,
    tiers: QuerySet[TicketTier],
    parents: t.Sequence[QuerySet[t.Any]],
) -> None:
    """Steps 2 and 3 of :func:`lock_for_cascade_delete`: passes, tiers, parents, then the passes and tiers again."""
    if passes is not None:
        _lock_rows(passes)
    _lock_rows(tiers)
    # NO KEY for an organization or a series, as EventSeries.delete() locks its own row: an event duplicate holds its
    # template event and inserts a copy whose commit-time foreign-key checks take KEY SHARE on them, so FOR UPDATE
    # here would wait for the duplicate while the duplicate waits for the event lock below. An event stays FOR UPDATE.
    # ponytail: NO KEY does not block a child insert, so a pass, an event or a series that someone creates for the
    # organization or the series during the delete can commit: the delete then fails on its foreign key, as before
    # #1538. Blocking it needs FOR UPDATE, and every way to take that here can deadlock with a path that holds a
    # row of this delete and inserts such a child.
    for rows in parents:
        _lock_rows(rows, no_key=rows.model in (Organization, EventSeries))
    if not parents:
        return
    # A pass or a tier that committed between the first lock of its model and the parent locks is locked now:
    # a purchase cannot add a holder to such a pass meanwhile, and step 4 sees the payments of the tier's
    # reservations. After the event locks, no new tier can commit (its insert waits at the foreign-key check on
    # the event). These second locks are out of the global order for those new rows only; a pass purchase or a
    # checkout on such a row that then waits for a row of this delete is a deadlock that Postgres detects.
    if passes is not None:
        _lock_rows(passes)
    _lock_rows(tiers)


def lock_for_bulk_delete(
    tickets: QuerySet[Ticket],
    *,
    tiers: QuerySet[TicketTier],
    parents: t.Sequence[QuerySet[t.Any]] = (),
    passes: QuerySet[SeriesPass] | None = None,
) -> None:
    """Lock the rows of several cascade deletes at once, in the global lock order, before a loop runs them.

    For a loop that runs a delete service once per row in one transaction (the admin bulk deletes). Each
    run locks only the rows of its own delete, so the loop alone would lock the Payment, SeriesPass,
    TicketTier and parent rows of the selection one delete at a time, not in pk order. One checkout can
    span tiers and events, and a series pass locks the tiers of several events in pk order, so that loop
    could deadlock with them. This takes steps 1 to 4 of :func:`lock_for_cascade_delete` for the whole
    selection. It ends no checkout: each delete service in the loop still does that for its own rows, so a
    delete that fails in its own savepoint expires nothing.
    """
    _lock_rows(Payment.objects.filter(ticket__in=tickets))
    _lock_delete_scope(passes, tiers, parents)
    # Step 4 for the whole selection: the rows that a reservation committed before the tier lock.
    _lock_rows(Payment.objects.filter(ticket__in=tickets))


def lock_for_cascade_delete(
    tickets: QuerySet[Ticket],
    *,
    tiers: QuerySet[TicketTier] | None = None,
    parents: t.Sequence[QuerySet[t.Any]] = (),
    passes: QuerySet[SeriesPass] | None = None,
) -> None:
    """End the unfinished checkouts of ``tickets`` that a delete is about to cascade away.

    Call it inside the deleting transaction, before the delete. It takes the locks of the delete in the
    global order (docs/engineering-notes.md, "Lock order"):

    1. the Payment rows of ``tickets``, pk order, and the statuses under that lock;
    2. the ``passes`` rows (``FOR UPDATE``, pk order): the series passes that the delete removes, which come
       before the tiers because a pass purchase locks its pass and then the tiers;
    3. every ``tiers`` row (``FOR UPDATE``, pk order), then the ``parents`` rows (pk order) in the order
       given: Organization, EventSeries, Event. The events and tiers take ``FOR UPDATE`` because the delete
       removes them: a transaction that inserts a child row meanwhile waits at its commit-time foreign-key
       check and then fails, instead of the delete failing on it. The organization and the series take
       ``FOR NO KEY UPDATE``: an event duplicate inserts a copy that references them while it holds its
       template event. With parents, the passes and the tiers are locked again after them, for a row that
       committed in between. A reservation locks its tier before it inserts, so after this no new Payment
       row can appear for these tiers;
    4. the Payment rows again, for the rows that a reservation committed between steps 1 and 3;
    5. the Ticket and DiscountCode rows (``release_uses_of_pending_tickets``) to give back the discount uses.

    After commit, the open Stripe Checkout sessions of the unfinished payments are expired: without their
    Payment rows a later ``checkout.session.completed`` has nothing to reconcile. A session stamp on a locked
    row waits for the delete, matches no row and expires the session itself (``stamp_session_or_expire``).
    A rollback discards the callback. Best effort: a failed expire is logged and the other sessions still go.

    Step 4, and the second pass and tier locks of step 3, are out of the global order for a row that committed
    between the first lock of its model and the second one. If a cancel, an expiry or a
    webhook holds such a row and waits for a tier of step 3, Postgres detects the deadlock and stops one of
    the two transactions. A session that is then paid with no Payment row is refunded in full
    (``refund_orphaned_checkout_session``).

    Args:
        tickets: The tickets that the delete removes.
        tiers: The tiers that the delete removes. None when it removes no tier (a ticket delete): steps 2 to 4
            are then skipped.
        parents: The parent rows that the delete removes, in lock order.
        passes: The series passes that the delete removes (a series or an organization delete). Only used
            with ``tiers``.
    """
    sessions_by_account = _lock_unfinished_sessions(tickets)
    if tiers is not None:
        _lock_delete_scope(passes, tiers, parents)
        for account_id, session_ids in _lock_unfinished_sessions(tickets).items():
            sessions_by_account[account_id] |= session_ids
    discount_code_service.release_uses_of_pending_tickets(tickets)
    if not sessions_by_account:
        return

    # A named closure, not functools.partial: the robust on_commit path logs a failure by __qualname__.
    def expire_deleted_checkout_sessions() -> None:
        for account_id, session_ids in sessions_by_account.items():
            expire_stripe_sessions_best_effort(sorted(session_ids), account_id)

    transaction.on_commit(expire_deleted_checkout_sessions, robust=True)


def _live_reservation_payments(reservation_id: UUID, *related: str) -> list[Payment]:
    """The reservation's still-chargeable rows: Payment AND its ticket both PENDING.

    Payment status alone is not enough for the session-create step: a buyer can
    cancel one ticket of the batch between reserve and session-create
    (cancel_ticket_by_user flips the Ticket and releases its tier slot but never
    touches the Payment — see _release_batch_tier_capacity). Charging such a row
    would capture money for a void ticket, so it must reach neither the Stripe
    line items nor the stamp; the expiry sweep reclaims it instead.

    Totally ordered on purpose. Each Stripe line item now carries its own row's
    amount (#739), so correctness no longer depends on row order — but Stripe
    replays an idempotency key only for byte-identical params, and an unordered
    query could hand a retry the same line items in a different sequence and turn
    it into an idempotency conflict (-> 500). ``created_at`` alone is not a total
    order: a batch's rows are bulk-created and share a timestamp, hence the ``id``
    tiebreak.

    Raises:
        HttpError: 404 when no live rows remain for the reservation.
    """
    payments = list(
        Payment.objects.select_related(*related)
        .filter(
            reservation_id=reservation_id,
            status=Payment.PaymentStatus.PENDING,
            ticket__status=Ticket.TicketStatus.PENDING,
        )
        .order_by("created_at", "id")
    )
    if not payments:
        raise HttpError(404, str(_("No pending reservation found.")))
    return payments


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
    #
    # select_for_update + materialize once: a plain SELECT never blocks under READ
    # COMMITTED, so a concurrent reclaim on the same rows (the cleanup_expired_payments
    # beat task, an overlapping payment_intent.canceled webhook) could both read
    # PENDING and both release tier capacity before either side deletes/updates. The
    # lock serializes that instead of racing it; deriving ticket_ids from this same
    # locked list (rather than re-querying) keeps everything downstream consistent
    # with the snapshot the lock was taken against (#632).
    batch_payments = list(
        Payment.objects.select_for_update(of=("self",))
        .filter(_batch_filter(payment), status=Payment.PaymentStatus.PENDING)
        .order_by("pk")
    )
    payment_ids = [p.id for p in batch_payments]
    ticket_ids = [p.ticket_id for p in batch_payments]

    # Pass row before tier rows (SeriesPassPurchaseService.purchase's lock order).
    # Ticket-based (not session-based): a reserved-but-not-sessioned series pass's
    # held_pass.stripe_session_id is "" and would be missed by a session lookup (#632).
    expire_held_passes_for_tickets(ticket_ids)

    # Release capacity per tier before the rows disappear.
    _release_batch_tier_capacity(ticket_ids)
    _release_batch_discount_uses(ticket_ids)

    # Clean up the pending tickets and payments in this batch. Non-PENDING
    # tickets (e.g. CANCELLED audit rows, past-event ACTIVE tickets) survive.
    Payment.objects.filter(pk__in=payment_ids).delete()
    Ticket.objects.filter(id__in=ticket_ids, status=Ticket.TicketStatus.PENDING).delete()


def resume_pending_checkout(
    payment_id: str,
    user: RevelUser,
) -> str:
    """Resume a pending Stripe checkout session by payment ID.

    Retrieves the existing Stripe checkout URL for a sessioned pending payment;
    for a reserved-but-not-sessioned batch (#632) it creates the Stripe session
    on demand (idempotent, keyed on reservation_id). Cleans up expired sessions
    and all tickets in the same batch.

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
    #
    # select_for_update + materialize once: a plain SELECT never blocks under READ
    # COMMITTED, so a concurrent reclaim on the same rows (the cleanup_expired_payments
    # beat task, an overlapping payment_intent.canceled webhook) could both read
    # PENDING and both release tier capacity before either side deletes/updates. The
    # lock serializes that instead of racing it; ticket_count/ticket_ids come from
    # this same locked list rather than re-querying (#632).
    batch_payments = list(
        Payment.objects.select_for_update(of=("self",))
        .filter(_batch_filter(payment), status=Payment.PaymentStatus.PENDING)
        .order_by("pk")
    )
    payment_ids = [p.id for p in batch_payments]
    ticket_ids = [p.ticket_id for p in batch_payments]
    ticket_count = len(ticket_ids)

    # Capture event ids before the ticket rows are deleted (a series-pass batch
    # spans multiple events; a plain batch has one).
    event_ids = set(Ticket.objects.filter(id__in=ticket_ids).values_list("event_id", flat=True))

    # Already-sessioned batch: best-effort expire the Stripe session after commit,
    # so the buyer's still-open checkout URL can't be paid once the Payment rows
    # (the webhook's reconciliation target) are deleted below (#632). It is the first
    # on_commit callback on this path, so an earlier failing callback cannot skip it.
    session_ids = sorted({p.stripe_session_id for p in batch_payments if p.stripe_session_id})
    if session_ids:
        first_ticket = Ticket.objects.filter(id__in=ticket_ids).select_related("event__organization").first()
        stripe_account_id = first_ticket.event.organization.stripe_account_id if first_ticket else None
        transaction.on_commit(functools.partial(expire_stripe_sessions_best_effort, session_ids, stripe_account_id))

    # Pass row before tier rows (SeriesPassPurchaseService.purchase's lock order).
    # Ticket-based (not session-based): a reserved-but-not-sessioned series pass's
    # held_pass.stripe_session_id is "" and would be missed by a session lookup (#632).
    expire_held_passes_for_tickets(ticket_ids)

    # Release capacity per tier before the rows disappear.
    _release_batch_tier_capacity(ticket_ids)
    _release_batch_discount_uses(ticket_ids)

    # Delete the pending tickets and payments in this batch. Non-PENDING tickets
    # (e.g. CANCELLED audit rows, past-event ACTIVE tickets) survive.
    Payment.objects.filter(pk__in=payment_ids).delete()
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
