"""Let ticket holders self-cancel the free tickets of one event.

Self-cancellation is gated by ``TicketTier.allow_user_cancellation`` (default False), and the
check reads the tier live, so flipping the event's FREE tiers makes every existing ticket on them
cancellable. Free tickets carry no refund, so ``refund_policy`` is left untouched.
"""

import typing as t
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from events.models import Event, TicketTier


class Command(BaseCommand):
    """Enable ``allow_user_cancellation`` on every FREE tier of an event."""

    help = "Allow ticket holders to cancel their free tickets for the given event"

    def add_arguments(self, parser: t.Any) -> None:
        """Add command arguments.

        Args:
            parser: The argument parser.
        """
        parser.add_argument("event_id", type=UUID, help="ID of the event whose free tiers to update.")
        parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Execute the update.

        Args:
            args: Positional arguments.
            options: Keyword arguments from command line.

        Raises:
            CommandError: If the event does not exist.
        """
        event_id: UUID = options["event_id"]
        if not Event.objects.filter(pk=event_id).exists():
            raise CommandError(f"Event {event_id} not found.")

        tiers = TicketTier.objects.filter(
            event_id=event_id,
            payment_method=TicketTier.PaymentMethod.FREE,
            allow_user_cancellation=False,
        )
        tier_ids = [str(pk) for pk in tiers.values_list("pk", flat=True)]
        if options["dry_run"]:
            self.stdout.write(f"[dry-run] Would enable self-cancellation on {len(tier_ids)} free tier(s): {tier_ids}")
            return

        updated = tiers.update(allow_user_cancellation=True)
        self.stdout.write(self.style.SUCCESS(f"Enabled self-cancellation on {updated} free tier(s): {tier_ids}"))
