"""Verify that every Celery-beat ``PeriodicTask`` points at a registered task.

An orphaned ``PeriodicTask`` references a task name string that no Celery task is
registered under — so the scheduler dispatches a message that no worker can route
(``NotRegistered``) and the job silently never runs. This happens when a task is
moved/renamed without keeping its registered ``name=`` (see ``scripts/check_task_names.py``,
which prevents new bare tasks), or when a row was created by hand against a wrong name.

Unlike the static ``task-names`` check, this inspects the live ``PeriodicTask`` rows in
the database, so it also catches manually-created beat tasks. Exits non-zero if any
orphan is found, so it can gate a deploy or run as a health check.
"""

import typing as t

from celery import current_app
from django.core.management.base import BaseCommand, CommandParser
from django_celery_beat.models import PeriodicTask


class Command(BaseCommand):
    help = "Fails if any PeriodicTask references a Celery task name that is not registered (orphaned beat task)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register the command's optional flags."""
        parser.add_argument(
            "--enabled-only",
            action="store_true",
            help="Only check enabled PeriodicTasks (ignore disabled rows).",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Report PeriodicTask rows whose task name is not in the Celery registry; exit 1 if any."""
        # Import every app's task modules so the registry is fully populated —
        # the same path a worker/beat process takes on boot.
        current_app.loader.import_default_modules()
        registered = set(current_app.tasks)

        periodic_tasks = PeriodicTask.objects.all().order_by("name")
        if options["enabled_only"]:
            periodic_tasks = periodic_tasks.filter(enabled=True)

        orphans = [pt for pt in periodic_tasks if pt.task not in registered]

        if not orphans:
            self.stdout.write(
                self.style.SUCCESS(f"✅ All {len(periodic_tasks)} PeriodicTask row(s) reference a registered task.")
            )
            return

        self.stderr.write(
            self.style.ERROR(f"❌ Found {len(orphans)} orphaned PeriodicTask row(s) (task name not registered):")
        )
        for pt in orphans:
            status = "enabled" if pt.enabled else "disabled"
            self.stderr.write(f"  - {pt.name!r} [{status}] → task={pt.task!r}")
        self.stderr.write(
            "\nThe referenced task was renamed/removed without keeping its registered name, or the row "
            "was created against a wrong name. Pin the task's name= (see scripts/check_task_names.py) or "
            "fix/remove the PeriodicTask row."
        )
        raise SystemExit(1)
