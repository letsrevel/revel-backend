"""Give back the capacity of an account's unfinished checkouts, before deleting the account cascades them away."""

import typing as t
from collections import Counter
from uuid import UUID

from django.db.models import F, Value
from django.db.models.functions import Greatest

from events.models import HeldSeriesPass, Payment, SeriesPass, Ticket, TicketTier

if t.TYPE_CHECKING:
    from accounts.models import RevelUser


def release_capacity_of_account(user: "RevelUser") -> None:
    """Give back the tier and series-pass slots of the unfinished checkouts that deleting ``user`` cascades away.

    Call it inside the deletion transaction, before ``user.delete()``. Only PENDING tickets and PENDING
    held passes give back their slot, like the checkout reclaim. An issued ticket or pass (ACTIVE or
    checked in) keeps its slot: deleting an account does not cancel what was bought, and giving the slot
    back could sell the same place twice.

    Locks follow the canonical order (Payment -> HeldSeriesPass -> SeriesPass -> TicketTier -> Ticket),
    each in pk order, and statuses are read only under those locks. Decrements are floored at zero.
    """
    # ponytail: a checkout that the user completes between this snapshot and ``user.delete()`` cascades
    # without giving its slot back. The account is being erased, so that window is tiny; closing it would
    # need a lock on the user row in every checkout path.
    ticket_ids = list(Ticket.objects.filter(user=user).values_list("pk", flat=True))
    held_ids = list(HeldSeriesPass.objects.filter(user=user).values_list("pk", flat=True))
    if not ticket_ids and not held_ids:
        return
    list(
        Payment.objects.select_for_update().filter(ticket_id__in=ticket_ids).order_by("pk").values_list("pk", flat=True)
    )
    list(HeldSeriesPass.objects.select_for_update().filter(pk__in=held_ids).order_by("pk").values_list("pk", flat=True))
    pass_ids = HeldSeriesPass.objects.filter(pk__in=held_ids).values_list("series_pass_id", flat=True)
    list(SeriesPass.objects.select_for_update().filter(pk__in=pass_ids).order_by("pk").values_list("pk", flat=True))
    tier_ids = Ticket.objects.filter(pk__in=ticket_ids).values_list("tier_id", flat=True)
    list(TicketTier.objects.select_for_update().filter(pk__in=tier_ids).order_by("pk").values_list("pk", flat=True))
    list(Ticket.objects.select_for_update().filter(pk__in=ticket_ids).order_by("pk").values_list("pk", flat=True))

    per_tier: Counter[UUID] = Counter(
        Ticket.objects.filter(pk__in=ticket_ids, status=Ticket.TicketStatus.PENDING).values_list("tier_id", flat=True)
    )
    for tier_id, count in sorted(per_tier.items()):
        TicketTier.objects.filter(pk=tier_id).update(quantity_sold=Greatest(F("quantity_sold") - count, Value(0)))

    per_pass: Counter[UUID] = Counter(
        HeldSeriesPass.objects.filter(pk__in=held_ids, status=HeldSeriesPass.HeldSeriesPassStatus.PENDING).values_list(
            "series_pass_id", flat=True
        )
    )
    for pass_id, count in sorted(per_pass.items()):
        SeriesPass.objects.filter(pk=pass_id).update(quantity_sold=Greatest(F("quantity_sold") - count, Value(0)))
