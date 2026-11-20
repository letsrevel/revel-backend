"""Post-rollout pass of the ``Ticket.unconfirmed_guest`` backfill (#1430).

During a rolling deploy the old pods keep inserting guest tickets with the column default (False), after
migration 0152 already ran. Run this ONCE after the rollout completes, with ``--before`` set to the time it
finished (dry-run first). Never re-run it later: an email-confirmed zero-total guest ticket created after the
deploy has the same shape as a legacy unconfirmed one, and would be flagged wrongly. Prints counts only.
"""

import datetime
import typing as t

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware

from events.models import Payment, Ticket
from events.utils.unconfirmed_guest import backfill_unconfirmed_guest


def _aware_datetime(value: str) -> datetime.datetime:
    """Parse an ISO 8601 datetime that carries a UTC offset."""
    try:
        parsed = parse_datetime(value)
    except ValueError:
        parsed = None
    if parsed is None or not is_aware(parsed):
        raise CommandError("--before must be an ISO 8601 datetime with a UTC offset, e.g. 2026-11-20T10:00:00+00:00.")
    return parsed


class Command(BaseCommand):
    """Flag paid guest-checkout tickets that never proved their email."""

    help = "Set Ticket.unconfirmed_guest on paid guest-checkout tickets created before --before (run once)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register ``--before`` and ``--dry-run``."""
        parser.add_argument("--before", help="Rollout-finished time, ISO 8601 with UTC offset (required).")
        parser.add_argument("--dry-run", action="store_true", help="Report the count without writing.")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Run (or count) the backfill for tickets created before the cutoff."""
        if not options["before"]:
            raise CommandError("--before is required.")
        before = _aware_datetime(options["before"])
        dry_run: bool = options["dry_run"]
        # One predicate for both modes: a dry run runs the UPDATE and rolls it back.
        with transaction.atomic():
            count = backfill_unconfirmed_guest(Ticket, Payment, created_before=before)
            if dry_run:
                transaction.set_rollback(True)
        verb = "Would flag" if dry_run else "Flagged"
        self.stdout.write(self.style.SUCCESS(f"{verb} {count} ticket(s) as unconfirmed_guest."))
