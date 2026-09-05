# src/events/management/commands/bootstrap_demo_video.py
"""Seed the five scenarios used to record the product demo videos.

Unlike ``bootstrap_events``, this command is fully idempotent and creates only
brand-new organizations, events, and ``@demovideo.example.com`` accounts. It never
reads or mutates the bootstrap / E2E fixtures, so it can run before them, after
them, or on its own against an empty-but-migrated database.
"""

import typing as t

import structlog
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from .demo_video_helpers import DEMO_PASSWORD, SCENARIOS, ScenarioSummary

logger = structlog.get_logger(__name__)

RULE = "=" * 78


class Command(BaseCommand):
    """Seed the demo-video scenarios and print the presenter's cheat sheet."""

    help = "Seed the five pre-arranged demo-video scenarios (idempotent, additive)."

    @transaction.atomic
    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Seed every scenario, then print the org, event, and credential summary.

        Raises:
            CommandError: If DEMO_MODE is not enabled.
        """
        # The seed publishes PUBLIC organizations owned by accounts whose password is
        # printed on screen, so it is gated the same way reset_events is. DEMO_MODE
        # defaults to DEBUG, and .env.example turns it on, so local runs are unaffected.
        if not settings.DEMO_MODE:
            raise CommandError(
                "This command can only be run when DEMO_MODE=True. "
                "Set DEMO_MODE=True in your environment to seed the demo-video scenarios."
            )

        logger.info("Seeding demo-video scenarios", count=len(SCENARIOS))
        summaries = [scenario() for scenario in SCENARIOS]
        self._print_summary(summaries)

    def _print_summary(self, summaries: list[ScenarioSummary]) -> None:
        """Print a readable, copy-pasteable rundown of everything that was seeded."""
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(RULE))
        self.stdout.write(self.style.MIGRATE_HEADING(f"DEMO VIDEO SEED — {len(summaries)} scenarios ready"))
        self.stdout.write(f"Every account below signs in with the password: {DEMO_PASSWORD}")
        self.stdout.write(self.style.MIGRATE_HEADING(RULE))

        for index, summary in enumerate(summaries, 1):
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(f"{index}. {summary.title}"))
            self.stdout.write(f"   org      /org/{summary.org_slug}")
            for path in summary.event_paths:
                self.stdout.write(f"   event    {path}")
            for account in summary.accounts:
                self.stdout.write(f"   · {account.email:<44} {account.name} — {account.role}")
            for note in summary.notes:
                self.stdout.write(self.style.WARNING(f"   ! {note}"))

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(RULE))
        self.stdout.write(
            self.style.SUCCESS(
                "Re-run this command any time — it refreshes the same rows and rolls every "
                "event date forward relative to today."
            )
        )
        self.stdout.write(self.style.MIGRATE_HEADING(RULE))
