"""Backfill fiscal ticket numbers for already-issued tickets (EU layer 1).

Numbers every ACTIVE/CHECKED_IN ticket per organization in ``created_at`` order and
seeds each organization's sequence, so new tickets continue the series without gaps.
``issued_at`` is set to ``created_at`` (the real activation time was never recorded).
Cancelled tickets stay unnumbered: whether they were ever issued is unknown.

Deliberately irreversible. Once numbers are on delivered tickets (PDFs, wallet passes),
clearing them and the per-org counters would let a re-apply, or the live numbering,
hand the same number to a different ticket. Refusing the rollback is the only option
that can never produce two tickets with one number; a no-op reverse would not help,
because rolling back 0126 drops the columns anyway.
"""

import re
import typing as t

from django.db import migrations

_ISSUED = ("active", "checked_in")
_BATCH = 1000


def _series_for(slug: str) -> str:
    # Mirrors events.service.ticket_number_service._series_for at the time of writing.
    return re.sub(r"[^A-Z0-9]", "", slug.upper())[:12] or "T"


def backfill(apps: t.Any, schema_editor: t.Any) -> None:
    """Number issued tickets per organization and seed each organization's sequence."""
    Organization = apps.get_model("events", "Organization")
    Ticket = apps.get_model("events", "Ticket")
    TicketNumberSequence = apps.get_model("events", "TicketNumberSequence")

    org_ids = (
        Ticket.objects.filter(status__in=_ISSUED, ticket_number__isnull=True)
        .order_by()  # drop the model's -created_at ordering so DISTINCT works on the org id alone
        .values_list("event__organization_id", flat=True)
        .distinct()
    )
    for org in Organization.objects.filter(pk__in=list(org_ids)).only("pk", "slug"):
        sequence, _ = TicketNumberSequence.objects.get_or_create(
            organization_id=org.pk, defaults={"series": _series_for(org.slug)}
        )
        tickets = list(
            Ticket.objects.filter(event__organization_id=org.pk, status__in=_ISSUED, ticket_number__isnull=True)
            .order_by("created_at", "pk")
            .only("pk", "created_at")
        )
        for ticket in tickets:
            sequence.last_number += 1
            ticket.ticket_series = sequence.series
            ticket.ticket_number = sequence.last_number
            ticket.issued_at = ticket.created_at
        Ticket.objects.bulk_update(tickets, ["ticket_series", "ticket_number", "issued_at"], batch_size=_BATCH)
        sequence.save(update_fields=["last_number"])

    # Cached PDFs/pkpasses of numbered tickets predate the compliance lines: invalidate them
    # so the next download renders the number (the content hash would differ anyway).
    Ticket.objects.filter(ticket_number__isnull=False).update(file_content_hash=None)


class Migration(migrations.Migration):
    dependencies = [("events", "0126_eu_compliance_ticket_numbers")]

    # No reverse_code: irreversible on purpose (see the module docstring).
    operations = [migrations.RunPython(backfill)]
