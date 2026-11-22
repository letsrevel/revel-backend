"""Registry and runner for post-deploy tasks (#1508).

A post-deploy task is a one-time operation that must run after a release is deployed (for example
syncing Stripe webhook subscriptions). Each app registers its tasks from ``AppConfig.ready`` with
:func:`register`. ``manage.py run_post_deploy`` calls :func:`run_pending`. This module imports no
other app, so ``common`` stays at the bottom of the dependency graph.

Semantics:

- A ``SUCCEEDED`` row is terminal: the task never runs again.
- ``FAILED`` and ``SKIPPED`` rows are retried on the next run. A task whose ``skip_reason`` returns a
  string is recorded as ``SKIPPED`` and evaluated again next time.
- A failing task is recorded and logged, and the runner continues with the next task. It never raises.

Locking: the runner locks the task row with ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a
transaction, not with a session advisory lock. Production runs behind PgBouncer in transaction
pooling, where session-level advisory locks are unsafe (the lock can stick to another client's server
connection). A row lock is transaction-scoped, and Postgres releases it if the process dies. A task
body runs in a savepoint, so its own database writes roll back when it fails.

Releases must be written as ``X.Y.Z`` (integers) so that they order numerically.

# ponytail: connection loss and BaseException are not caught. The outer transaction rolls back, the command
# exits non-zero, and the next run retries. Tasks must be idempotent.
# ponytail: no per-task timeout; tasks rely on their client timeouts (Stripe). Upgrade path:
# statement_timeout or a subprocess with a deadline.
"""

import dataclasses
import re
import typing as t

import structlog
from django.core.exceptions import ImproperlyConfigured
from django.db import models, transaction
from django.utils import timezone

from common.models import PostDeployTaskRun
from common.utils import get_or_create_with_race_protection

logger = structlog.get_logger(__name__)

MAX_OUTPUT_LENGTH = 4000

Outcome = t.Literal["succeeded", "failed", "skipped", "already_done", "busy", "would_run"]


def _never_skip() -> str | None:
    return None


@dataclasses.dataclass(frozen=True)
class PostDeployTask:
    """A one-time task to run after a release is deployed.

    Attributes:
        id: Stable unique identifier, stored in the database.
        release: Release that ships the task, as ``X.Y.Z``.
        description: One-line summary for humans.
        run: The task body. It returns a short text that the runner stores as output.
        skip_reason: Returns a reason string when the task must not run now, else ``None``.
    """

    id: str
    release: str
    description: str
    run: t.Callable[[], str]
    skip_reason: t.Callable[[], str | None] = _never_skip


class PostDeployResult(t.TypedDict):
    """Outcome of one task in a runner pass."""

    id: str
    outcome: Outcome
    detail: str


_REGISTRY: dict[str, PostDeployTask] = {}


def register(task: PostDeployTask) -> None:
    """Add a task to the registry.

    Args:
        task: The task to register.

    Raises:
        ImproperlyConfigured: If the release is not ``X.Y.Z`` or a task with the same id is already registered.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", task.release):
        raise ImproperlyConfigured(f"Post-deploy task {task.id!r} has release {task.release!r}; use X.Y.Z.")
    if task.id in _REGISTRY:
        raise ImproperlyConfigured(f"Post-deploy task {task.id!r} is already registered.")
    _REGISTRY[task.id] = task


def _release_key(release: str) -> tuple[int, ...]:
    return tuple(int(part) for part in release.split("."))


def registered() -> list[PostDeployTask]:
    """Return the registered tasks ordered by release (numeric), then id."""
    return sorted(_REGISTRY.values(), key=lambda task: (_release_key(task.release), task.id))


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_LENGTH else text[: MAX_OUTPUT_LENGTH - 3] + "..."


def _execute(task: PostDeployTask, row: PostDeployTaskRun) -> PostDeployResult:
    """Run one task under the row lock and record the result on ``row``."""
    counted = False
    try:
        with transaction.atomic():  # savepoint: a database error here must not break the outer transaction
            reason = task.skip_reason()
        if reason is not None:
            row.status, row.output, row.started_at = PostDeployTaskRun.Status.SKIPPED, _truncate(reason), None
            row.finished_at = timezone.now()
            row.save()
            return {"id": task.id, "outcome": "skipped", "detail": reason}
        row.attempts += 1
        counted = True
        row.started_at = timezone.now()
        with transaction.atomic():  # savepoint: the body's own writes roll back on failure
            output = task.run()
    except Exception as exc:
        if not counted:  # skip_reason raised: count the attempt
            row.attempts += 1
        detail = f"{type(exc).__name__}: {exc}"
        row.status, row.output = PostDeployTaskRun.Status.FAILED, _truncate(detail)
        logger.error(
            "post_deploy_task_failed",
            task_id=task.id,
            release=task.release,
            error=type(exc).__name__,
            detail=_truncate(detail),
        )
        outcome: Outcome = "failed"
    else:
        row.status, row.output = PostDeployTaskRun.Status.SUCCEEDED, _truncate(output)
        detail, outcome = output, "succeeded"
    row.finished_at = timezone.now()
    row.save()
    return {"id": task.id, "outcome": outcome, "detail": detail}


def _run_one(task: PostDeployTask) -> PostDeployResult:
    row, _ = get_or_create_with_race_protection(
        PostDeployTaskRun,
        models.Q(task_id=task.id),
        {"task_id": task.id, "release": task.release, "status": PostDeployTaskRun.Status.FAILED, "attempts": 0},
    )
    with transaction.atomic():
        locked = PostDeployTaskRun.objects.select_for_update(skip_locked=True).filter(pk=row.pk).first()
        if locked is None:
            return {"id": task.id, "outcome": "busy", "detail": "another process is running this task"}
        if locked.status == PostDeployTaskRun.Status.SUCCEEDED:
            return {"id": task.id, "outcome": "already_done", "detail": ""}
        return _execute(task, locked)


def run_pending(dry_run: bool = False) -> list[PostDeployResult]:
    """Run every registered task that has not succeeded yet, in order.

    Args:
        dry_run: When true, only report what would happen and write nothing.

    Returns:
        One result per registered task.
    """
    results: list[PostDeployResult] = []
    for task in registered():
        if PostDeployTaskRun.objects.filter(task_id=task.id, status=PostDeployTaskRun.Status.SUCCEEDED).exists():
            results.append({"id": task.id, "outcome": "already_done", "detail": ""})
        elif dry_run:
            try:
                reason = task.skip_reason()
            except Exception as exc:
                results.append({"id": task.id, "outcome": "failed", "detail": f"{type(exc).__name__}: {exc}"})
                continue
            results.append(
                {"id": task.id, "outcome": "skipped", "detail": reason}
                if reason is not None
                else {"id": task.id, "outcome": "would_run", "detail": task.description}
            )
        else:
            results.append(_run_one(task))
    return results
