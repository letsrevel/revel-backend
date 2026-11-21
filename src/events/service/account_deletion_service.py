"""Give back the capacity of an account's unfinished checkouts, before deleting the account cascades them away."""

import typing as t
from collections import Counter

from django.db.models import F, Value
from django.db.models.functions import Greatest

from events.models import HeldSeriesPass, Payment, SeriesPass, Ticket, TicketTier, WaitlistOffer
from events.service.discount_code_service import release_discount_uses
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
    locked_passes = list(HeldSeriesPass.objects.select_for_update(of=("self",)).filter(pk__in=held_ids).order_by("pk"))
    pass_ids = sorted({held.series_pass_id for held in locked_passes})
    list(
        SeriesPass.objects.select_for_update(of=("self",))
        .filter(pk__in=pass_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    pending = [
        ticket
        for ticket in lock_tier_then_ticket(Ticket.objects.filter(pk__in=ticket_ids))
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
