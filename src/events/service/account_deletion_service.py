"""Give back the capacity of a deleted account's unfinished checkouts, and expire their open Stripe sessions."""

import typing as t
from collections import Counter, defaultdict

import structlog
from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest

from events.models import HeldSeriesPass, Payment, SeriesPass, Ticket, TicketTier, WaitlistOffer
from events.service.discount_code_service import release_discount_uses
from events.service.pending_checkout import expire_stripe_sessions_best_effort
from events.service.ticket_service import lock_tier_then_ticket
from events.service.waitlist_service import enqueue_waitlist_processing

if t.TYPE_CHECKING:
    from accounts.models import RevelUser


def release_capacity_of_account(user: "RevelUser") -> None:
    """Give back the tier and series-pass slots of the unfinished checkouts that deleting ``user`` cascades away.

    Call it inside the deletion transaction, before ``user.delete()``. Only PENDING tickets and PENDING
    held passes give back their slot, like the checkout reclaim. An issued ticket or pass (ACTIVE or
    checked in) keeps its slot: deleting an account does not cancel what was bought, and giving the slot
    back could sell the same place twice.

    One locked snapshot drives every release: the canonical lock order (Payment -> HeldSeriesPass ->
    SeriesPass -> TicketTier -> Ticket, each in pk order, then the DiscountCode rows), with statuses read
    only under those locks. Decrements are floored at zero, and the freed events get waitlist processing.
    After commit, the open Stripe Checkout sessions of the pending payments and passes are expired.
    """
    # ponytail: a checkout, pass purchase or waitlist offer that inserts a new PENDING row for this account
    # after the snapshot below is cascaded by ``user.delete()`` without giving its slot back or starting the
    # waitlist. The account is being erased, so the window is tiny; closing it would need a lock on the user
    # row in every checkout path.
    ticket_ids = list(Ticket.objects.filter(user=user).values_list("pk", flat=True))
    held_ids = list(HeldSeriesPass.objects.filter(user=user).values_list("pk", flat=True))
    # The account's PENDING waitlist offers hold places too, and they cascade with it.
    offer_event_ids = set(
        WaitlistOffer.objects.filter(user=user, status=WaitlistOffer.WaitlistOfferStatus.PENDING).values_list(
            "event_id", flat=True
        )
    )
    list(
        Payment.objects.select_for_update(of=("self",))
        .filter(ticket_id__in=ticket_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    locked_passes = list(
        HeldSeriesPass.objects.select_for_update(of=("self",)).filter(pk__in=held_ids, user=user).order_by("pk")
    )
    pass_ids = sorted({held.series_pass_id for held in locked_passes})
    list(
        SeriesPass.objects.select_for_update(of=("self",))
        .filter(pk__in=pass_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    pending = [
        ticket
        for ticket in lock_tier_then_ticket(Ticket.objects.filter(pk__in=ticket_ids, user=user))
        if ticket.status == Ticket.TicketStatus.PENDING
    ]

    for tier_id, count in sorted(Counter(ticket.tier_id for ticket in pending).items()):
        TicketTier.objects.filter(pk=tier_id).update(quantity_sold=Greatest(F("quantity_sold") - count, Value(0)))
    release_discount_uses(ticket.discount_code_id for ticket in pending)

    pending_passes = (held for held in locked_passes if held.status == HeldSeriesPass.HeldSeriesPassStatus.PENDING)
    for pass_id, count in sorted(Counter(held.series_pass_id for held in pending_passes).items()):
        SeriesPass.objects.filter(pk=pass_id).update(quantity_sold=Greatest(F("quantity_sold") - count, Value(0)))

    for event_id in sorted({ticket.event_id for ticket in pending} | offer_event_ids):
        enqueue_waitlist_processing(event_id)

    _expire_open_sessions_after_commit(user, ticket_ids, [held.pk for held in locked_passes])


def _expire_open_sessions_after_commit(user: "RevelUser", ticket_ids: list[t.Any], held_ids: list[t.Any]) -> None:
    """After commit, expire the Stripe Checkout sessions of the PENDING payments and passes that cascade away.

    Without its Payment rows, a later ``checkout.session.completed`` has nothing to reconcile, so the
    session must not stay payable. Best effort, like the other paths that end a pending checkout: a
    failed expire is logged with the session id and the user id, and the other sessions still go.
    """
    sessions_by_account: dict[str | None, set[str]] = defaultdict(set)
    payment_sessions = (
        Payment.objects.filter(Q(ticket_id__in=ticket_ids) | Q(user=user), status=Payment.PaymentStatus.PENDING)
        .exclude(stripe_session_id="")
        .values_list("stripe_session_id", "ticket__event__organization__stripe_account_id")
    )
    pass_sessions = (
        HeldSeriesPass.objects.filter(pk__in=held_ids, status=HeldSeriesPass.HeldSeriesPassStatus.PENDING)
        .exclude(stripe_session_id="")
        .values_list("stripe_session_id", "series_pass__event_series__organization__stripe_account_id")
    )
    for session_id, account_id in [*payment_sessions, *pass_sessions]:
        sessions_by_account[account_id].add(session_id)
    if not sessions_by_account:
        return
    user_id = str(user.pk)

    # A named closure, not functools.partial: the robust on_commit path logs a failure by __qualname__.
    def expire_account_sessions() -> None:
        with structlog.contextvars.bound_contextvars(user_id=user_id):
            for account_id, session_ids in sessions_by_account.items():
                expire_stripe_sessions_best_effort(sorted(session_ids), account_id)

    transaction.on_commit(expire_account_sessions, robust=True)
