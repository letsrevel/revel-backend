"""``python manage.py run_post_deploy [--dry-run]``: run pending post-deploy tasks (#1508)."""

import typing as t

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from common.post_deploy import run_pending


class Command(BaseCommand):
    """Run the registered post-deploy tasks that have not succeeded yet."""

    help = "Run pending post-deploy tasks. Failures are recorded and logged; the exit code is always 0."

    def add_arguments(self, parser: CommandParser) -> None:
        """Wire ``--dry-run``."""
        parser.add_argument("--dry-run", action="store_true", help="Report what would run. Write nothing.")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Run the pending tasks and print one line per task."""
        dry_run: bool = options["dry_run"]
        if not settings.FEATURE_POST_DEPLOY_TASKS and not dry_run:
            self.stdout.write("post-deploy tasks are disabled (FEATURE_POST_DEPLOY_TASKS=False); nothing ran")
            return
        for result in run_pending(dry_run=dry_run):
            line = f"{result['id']}: {result['outcome']}"
            self.stdout.write(f"{line} ({result['detail']})" if result["detail"] else line)
