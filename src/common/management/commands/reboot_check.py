"""Check whether it is safe to reboot the server.

Inspects Celery workers for active tasks and the Redis broker for queued
messages. Exits with code 0 when safe, 1 when not.

Usage (inside the web or celery container):
    python manage.py reboot_check
    python manage.py reboot_check --wait          # poll until safe
    python manage.py reboot_check --wait --timeout 300
"""

import time
import typing as t

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Check whether it is safe to reboot the server (no active/queued Celery tasks)."

    def add_arguments(self, parser: t.Any) -> None:
        """Add CLI arguments."""
        parser.add_argument(
            "--wait",
            action="store_true",
            help="Poll until safe instead of a single check.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=600,
            help="Max seconds to wait when --wait is used (default: 600).",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=5,
            help="Seconds between polls when --wait is used (default: 5).",
        )

    def handle(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Run the reboot readiness check."""
        wait: bool = kwargs["wait"]
        timeout: int = kwargs["timeout"]
        interval: int = kwargs["interval"]

        if wait:
            self._wait_until_safe(timeout, interval)
        else:
            safe, report = self._check()
            self.stdout.write(report)
            if not safe:
                raise SystemExit(1)

    def _wait_until_safe(self, timeout: int, interval: int) -> None:
        """Poll until no active or queued tasks, or timeout."""
        deadline = time.monotonic() + timeout
        self.stdout.write(self.style.WARNING(f"Waiting up to {timeout}s for workers to drain..."))

        while time.monotonic() < deadline:
            safe, report = self._check()
            self.stdout.write(report)
            if safe:
                return
            remaining = int(deadline - time.monotonic())
            self.stdout.write(self.style.WARNING(f"Not safe yet. Retrying in {interval}s ({remaining}s remaining)..."))
            time.sleep(interval)

        self.stdout.write(self.style.ERROR("Timeout reached — tasks did not drain in time."))
        raise SystemExit(1)

    def _check(self) -> tuple[bool, str]:
        """Return (is_safe, human_readable_report)."""
        active_tasks = self._get_active_tasks()
        queued_count = self._get_queue_depth()

        lines: list[str] = []
        safe = True

        # Active tasks
        if active_tasks is None:
            lines.append(self.style.ERROR("Could not reach Celery workers (are they running?)."))
            safe = False
        elif active_tasks:
            safe = False
            total = sum(len(tasks) for tasks in active_tasks.values())
            lines.append(self.style.ERROR(f"Active tasks: {total}"))
            for worker, tasks in active_tasks.items():
                for task in tasks:
                    name = task.get("name", "unknown")
                    task_id = task.get("id", "?")
                    lines.append(f"  {worker}: {name} ({task_id})")
        else:
            lines.append(self.style.SUCCESS("Active tasks: 0"))

        # Queued tasks
        if queued_count > 0:
            safe = False
            lines.append(self.style.ERROR(f"Queued tasks: {queued_count}"))
        else:
            lines.append(self.style.SUCCESS("Queued tasks: 0"))

        # Verdict
        if safe:
            lines.append(self.style.SUCCESS("Safe to reboot."))
        else:
            lines.append(self.style.ERROR("NOT safe to reboot."))

        return safe, "\n".join(lines)

    def _get_active_tasks(self) -> dict[str, list[t.Any]] | None:
        """Inspect workers for currently executing tasks."""
        from revel.celery import app

        inspect = app.control.inspect(timeout=5.0)
        try:
            active = inspect.active()
        except Exception:
            return None
        return active

    def _get_queue_depth(self) -> int:
        """Check Redis for pending messages in the default Celery queue."""
        import redis

        try:
            r = redis.Redis.from_url(settings.CELERY_BROKER_URL)
            return r.llen("celery")  # type: ignore[return-value]
        except Exception:
            return -1
