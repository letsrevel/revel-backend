"""Backfill of ``Ticket.unconfirmed_guest`` (#1430), shared by migration 0152 and the management command."""

import datetime
import typing as t

from django.db.models import Exists, OuterRef, Q


def backfill_unconfirmed_guest(
    ticket_model: t.Any, payment_model: t.Any, *, created_before: datetime.datetime | None = None
) -> int:
    """Flag paid guest-checkout tickets issued without email proof; returns the number of rows updated.

    Takes the model classes so a migration can pass its historical models. Idempotent: it only ever sets
    the flag, and skips rows that already have it. ``created_before`` limits the update to tickets created
    before that instant (the management command requires it, see there).
    """
    # migration 0152 calls this; keep the base predicate frozen
    qs = ticket_model.objects.filter(
        Q(sale_source__isnull=True) | Q(sale_source="checkout"),
        Q(tier__payment_method="online") | Exists(payment_model.objects.filter(ticket_id=OuterRef("pk"))),
        user__guest=True,
        unconfirmed_guest=False,
    )
    if created_before is not None:
        qs = qs.filter(created_at__lt=created_before)
    updated: int = qs.update(unconfirmed_guest=True)
    return updated
