# src/events/management/commands/reset_events.py

import typing as t

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q

from accounts.models import RevelUser
from events.models import Organization
from questionnaires.models import Questionnaire


class Command(BaseCommand):
    """Reset demo data by deleting all organizations and example.com users, then re-bootstrapping.

    This command is only available when settings.DEMO_MODE is True to prevent accidental
    data deletion in production environments.
    """

    help = "Delete all organizations and @example.com users, then run bootstrap_events (DEMO_MODE only)"

    def add_arguments(self, parser: t.Any) -> None:
        """Add command arguments.

        Args:
            parser: The argument parser.
        """
        parser.add_argument(
            "--no-input",
            action="store_true",
            help="Skip confirmation prompt (useful for periodic tasks)",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Execute the reset_events command.

        Args:
            args: Positional arguments.
            options: Keyword arguments from command line.

        Raises:
            CommandError: If DEMO_MODE is not enabled.
        """
        # Safety check: only run in DEMO_MODE
        if not settings.DEMO_MODE:
            raise CommandError(
                "This command can only be run when DEMO_MODE=True. "
                "Set DEMO_MODE=True in your environment to use this command."
            )

        # Confirmation prompt (unless --no-input is specified)
        if not options["no_input"]:
            confirmation = input(
                self.style.WARNING(
                    "This will DELETE all organizations and all users with @example.com emails, "
                    "then re-run bootstrap_events.\n"
                    "Are you sure you want to continue? (yes/no): "
                )
            )
            if confirmation.lower() != "yes":
                self.stdout.write(self.style.ERROR("Reset cancelled."))
                return

        # Perform the reset in a transaction
        # We clear on_commit hooks to prevent notification signals from failing when trying to
        # access deleted users (cascade delete triggers signals but users are already gone)
        with transaction.atomic():
            # Count objects before deletion
            org_count = Organization.objects.count()
            user_count = RevelUser.objects.filter(~Q(email__endswith="@letsrevel.io")).count()

            self.stdout.write(
                self.style.WARNING(f"Deleting {org_count} organizations and {user_count} @example.com users...")
            )

            # Delete all organizations (cascade will handle related objects)
            Organization.objects.all().delete()
            Questionnaire.objects.all().delete()
            self.stdout.write(self.style.SUCCESS(f"✓ Deleted {org_count} organizations"))

            # Delete all users with @example.com emails
            RevelUser.objects.filter(~Q(email__endswith="@letsrevel.io")).delete()
            self.stdout.write(self.style.SUCCESS(f"✓ Deleted {user_count} @example.com users"))

            # Clear on_commit hooks to prevent signals from trying to access deleted users
            # This prevents "DoesNotExist" errors when signals try to access cascade-deleted users
            connection.run_on_commit = []

        # Re-bootstrap (outside transaction to allow bootstrap's own transaction handling)
        self.stdout.write(self.style.MIGRATE_HEADING("Running bootstrap_events..."))
        call_command("bootstrap_events")

        self.stdout.write(
            self.style.SUCCESS(
                "\n✓ Reset complete! All organizations and @example.com users have been "
                "deleted and fresh demo data has been created."
            )
        )
