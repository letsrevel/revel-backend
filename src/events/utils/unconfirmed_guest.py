"""Backfill of ``Ticket.unconfirmed_guest`` (#1430), shared by migration 0152 and the management command."""

import typing as t

from django.db.models import Exists, OuterRef, Q


def backfill_unconfirmed_guest(ticket_model: t.Any, payment_model: t.Any) -> int:
    """Flag paid guest-checkout tickets issued without email proof; returns the number of rows updated.

    Takes the model classes so a migration can pass its historical models. Idempotent: it only ever sets
    the flag, and skips rows that already have it.
    """
    updated: int = ticket_model.objects.filter(
        Q(sale_source__isnull=True) | Q(sale_source="checkout"),
        Q(tier__payment_method="online") | Exists(payment_model.objects.filter(ticket_id=OuterRef("pk"))),
        user__guest=True,
        unconfirmed_guest=False,
    ).update(unconfirmed_guest=True)
    return updated
