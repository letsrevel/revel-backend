"""``python manage.py post_deploy_status [--check]``: show post-deploy task state (#1508)."""

import typing as t

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from common.models import PostDeployTaskRun
from common.post_deploy import registered


class Command(BaseCommand):
    """Print the state of every registered post-deploy task."""

    help = "Show post-deploy task status. With --check, exit 1 if a registered task is failed, running or pending."

    def add_arguments(self, parser: CommandParser) -> None:
        """Wire ``--check``."""
        parser.add_argument(
            "--check", action="store_true", help="Fail if a registered task is FAILED, running or pending."
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Print one row per registered task, then rows whose task is no longer registered."""
        # A runner holds the row lock for the whole attempt, so a row we cannot lock is running now,
        # whatever status its last committed state shows, unless that status is SUCCEEDED.
        # The locks last only for this read.
        with transaction.atomic():
            rows = {row.task_id: row for row in PostDeployTaskRun.objects.all()}
            # Rows we can lock are read again here: a retry can commit between the two reads, so only
            # the locking read is current. The plain read tells us which rows exist but are locked.
            free = {row.task_id: row for row in PostDeployTaskRun.objects.select_for_update(skip_locked=True)}
        busy = rows.keys() - free.keys()
        rows.update(free)
        problems: list[str] = []
        self.stdout.write(f"{'TASK':<40} {'RELEASE':<10} {'STATUS':<12} {'ATTEMPTS':<9} FINISHED")
        for task in registered():
            row = rows.pop(task.id, None)
            status = row.status if row else "pending"
            if row and row.status != PostDeployTaskRun.Status.SUCCEEDED:  # SUCCEEDED is terminal: no runner retries it
                if task.id in busy or (row.finished_at is None and row.status == PostDeployTaskRun.Status.FAILED):
                    status = "running"  # locked by a runner, or created and not finished (in progress or crashed)
            if status in ("pending", "running", PostDeployTaskRun.Status.FAILED):
                problems.append(f"{task.id} ({status})")
            finished = row.finished_at.isoformat() if row and row.finished_at else "-"
            self.stdout.write(
                f"{task.id:<40} {task.release:<10} {status:<12} {row.attempts if row else 0:<9} {finished}"
            )
        for row in rows.values():
            finished = row.finished_at.isoformat() if row.finished_at else "-"
            self.stdout.write(f"{row.task_id:<40} {row.release:<10} {'unregistered':<12} {row.attempts:<9} {finished}")
        if options["check"] and problems:
            raise CommandError(f"Post-deploy tasks need attention: {', '.join(problems)}")
