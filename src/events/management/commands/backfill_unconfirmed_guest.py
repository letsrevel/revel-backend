"""Re-run the ``Ticket.unconfirmed_guest`` backfill (#1430).

During a rolling deploy the old pods keep inserting guest tickets with the column default (False), after
migration 0152 already ran. Run this once after the rollout completes. Idempotent.
"""

import typing as t

from django.core.management.base import BaseCommand

from events.models import Payment, Ticket
from events.utils.unconfirmed_guest import backfill_unconfirmed_guest


class Command(BaseCommand):
    """Flag paid guest-checkout tickets that never proved their email."""

    help = "Set Ticket.unconfirmed_guest on paid guest-checkout tickets issued before the flag existed (idempotent)"

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Run the backfill and report the row count."""
        count = backfill_unconfirmed_guest(Ticket, Payment)
        self.stdout.write(self.style.SUCCESS(f"Flagged {count} ticket(s) as unconfirmed_guest."))
