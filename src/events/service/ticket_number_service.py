"""Per-organization fiscal ticket numbering (EU layer 1, #1060/#1061/#1064).

Numbers are assigned without gaps and never reused. Holes appear only if numbered
tickets are later deleted (event / tier / user cascade, see #1068).

A ticket gets its number when it is first *issued* — the moment it becomes ACTIVE or
CHECKED_IN — never while PENDING: abandoned online checkouts are deleted, which would
leave holes in the series. Numbers are handed out under a row lock on the
organization's :class:`~events.models.TicketNumberSequence`, inside the caller's
transaction, so a rollback returns both the tickets and the counter. That relies on
every production entry point running atomically: requests (ATOMIC_REQUESTS), the
Django admin, and the series-pass Celery task.

Entry points: the ``Ticket`` post_save receiver (every ``save()`` path) and the bulk
writers that bypass signals (batch checkout, series-pass materialization/activation).
"""

import re
import typing as t
from collections import defaultdict
from uuid import UUID

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from events.models import Organization, Ticket, TicketNumberSequence

ISSUED_STATUSES: t.Final = (Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN)
_SERIES_SLUG_CHARS = 12


def format_ticket_number(ticket: Ticket) -> str:
    """Human-readable number, e.g. ``MYORG-000123``; empty while unassigned."""
    if ticket.ticket_number is None:
        return ""
    return f"{ticket.ticket_series}-{ticket.ticket_number:06d}"


def _series_for(org: Organization) -> str:
    """Derive an organization's series from its slug (letters and digits, upper-cased).

    The series is unique per organization, not globally: two orgs can share one. Any
    future lookup by ticket number must be scoped to an organization.
    """
    return re.sub(r"[^A-Z0-9]", "", org.slug.upper())[:_SERIES_SLUG_CHARS] or "T"


def _lock_sequence(org_id: UUID) -> TicketNumberSequence:
    """Get-or-create the org's sequence row and lock it.

    Race-safe via INSERT ... ON CONFLICT DO NOTHING. ``get_or_create_with_race_protection``
    is not used because the row must be locked anyway, so the locking SELECT doubles as
    the get.
    """
    org = Organization.objects.only("slug").get(pk=org_id)
    TicketNumberSequence.objects.bulk_create(
        [TicketNumberSequence(organization_id=org_id, series=_series_for(org))], ignore_conflicts=True
    )
    return TicketNumberSequence.objects.select_for_update().get(organization_id=org_id)


def assign_ticket_numbers(tickets: t.Iterable[Ticket]) -> None:
    """Number every issued, still-unnumbered ticket in ``tickets``, in creation order.

    Idempotent and safe to call on any mix of tickets: pending, cancelled and
    already-numbered ones are skipped. The passed instances are updated in place.

    Locks follow the canonical order parent row -> TicketTier (pk) -> Ticket ->
    TicketNumberSequence (docs/engineering-notes.md): callers must lock any tiers before
    numbering; here the tickets are locked, then the per-org sequence rows in org-id order.

    The per-org sequence lock is held until the caller's transaction commits, so callers
    must not do network I/O after numbering.

    Args:
        tickets: Saved ``Ticket`` instances.
    """
    candidates = {tk.pk: tk for tk in tickets if tk.pk and tk.ticket_number is None and tk.status in ISSUED_STATUSES}
    if not candidates:
        return

    with transaction.atomic():
        locked = list(
            Ticket.objects.select_for_update(of=("self",))
            .filter(pk__in=candidates, ticket_number__isnull=True, status__in=ISSUED_STATUSES)
            .annotate(org_id=F("event__organization_id"))
            .order_by("created_at", "pk")
        )
        by_org: dict[UUID, list[Ticket]] = defaultdict(list)
        for row in locked:
            by_org[row.org_id].append(row)

        now = timezone.now()
        for org_id in sorted(by_org):
            sequence = _lock_sequence(org_id)
            for ticket in by_org[org_id]:
                sequence.last_number += 1
                ticket.ticket_series, ticket.ticket_number, ticket.issued_at = (
                    sequence.series,
                    sequence.last_number,
                    now,
                )
            Ticket.objects.bulk_update(by_org[org_id], ["ticket_series", "ticket_number", "issued_at"])
            sequence.save(update_fields=["last_number"])

    for numbered in locked:
        original = candidates[numbered.pk]
        original.ticket_series, original.ticket_number, original.issued_at = (
            numbered.ticket_series,
            numbered.ticket_number,
            numbered.issued_at,
        )
