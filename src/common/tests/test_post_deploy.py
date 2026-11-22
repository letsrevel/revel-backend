"""Tests for the post-deploy task registry and runner (#1508)."""

import threading
import typing as t
from collections.abc import Iterator

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from structlog.testing import capture_logs

from common import post_deploy
from common.models import PostDeployTaskRun
from common.post_deploy import PostDeployTask, register, registered, run_pending

Status = PostDeployTaskRun.Status


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, PostDeployTask]]:
    """Swap the module registry for an empty one so the real tasks do not interfere."""
    fresh: dict[str, PostDeployTask] = {}
    monkeypatch.setattr(post_deploy, "_REGISTRY", fresh)
    yield fresh


def _task(task_id: str, release: str = "1.0.0", **kwargs: t.Any) -> PostDeployTask:
    kwargs.setdefault("run", lambda: "ok")
    return PostDeployTask(id=task_id, release=release, description="d", **kwargs)


def test_registered_orders_by_release_then_id() -> None:
    for task in (_task("b", "3.10.0"), _task("z", "3.9.0"), _task("a", "3.10.0")):
        register(task)
    assert [task.id for task in registered()] == ["z", "a", "b"]


def test_duplicate_id_rejected() -> None:
    register(_task("a"))
    with pytest.raises(ImproperlyConfigured):
        register(_task("a"))


@pytest.mark.parametrize("release", ["3.1", "v3.1.0", "3.1.0rc1", ""])
def test_release_must_be_x_y_z(release: str) -> None:
    with pytest.raises(ImproperlyConfigured):
        register(_task("a", release))


@pytest.mark.django_db
def test_success_is_recorded() -> None:
    register(_task("a", run=lambda: "done"))
    results = run_pending()
    assert results == [{"id": "a", "outcome": "succeeded", "detail": "done"}]
    row = PostDeployTaskRun.objects.get(task_id="a")
    assert (row.status, row.attempts, row.output, row.release) == (Status.SUCCEEDED, 1, "done", "1.0.0")
    assert row.started_at is not None
    assert row.finished_at is not None


@pytest.mark.django_db
def test_failure_is_recorded_logged_and_isolated() -> None:
    def boom() -> str:
        PostDeployTaskRun.objects.create(task_id="side-effect", release="9.9.9")
        raise ValueError("bad")

    register(_task("a", "1.0.0", run=boom))
    register(_task("b", "1.0.1"))
    with capture_logs() as logs:
        results = run_pending()
    assert [r["outcome"] for r in results] == ["failed", "succeeded"]
    row = PostDeployTaskRun.objects.get(task_id="a")
    assert (row.status, row.attempts, row.output) == (Status.FAILED, 1, "ValueError: bad")
    assert row.finished_at is not None
    assert any(
        log["event"] == "post_deploy_task_failed"
        and log["task_id"] == "a"
        and log["error"] == "ValueError"
        and log["detail"] == "ValueError: bad"
        for log in logs
    )
    assert not PostDeployTaskRun.objects.filter(task_id="side-effect").exists()


@pytest.mark.django_db
def test_succeeded_is_not_rerun() -> None:
    calls: list[int] = []

    def body() -> str:
        calls.append(1)
        return "ok"

    register(_task("a", run=body))
    run_pending()
    results = run_pending()
    assert results[0]["outcome"] == "already_done"
    assert calls == [1]
    assert PostDeployTaskRun.objects.get(task_id="a").attempts == 1


@pytest.mark.django_db
def test_failed_is_retried() -> None:
    state = {"fail": True}

    def body() -> str:
        if state["fail"]:
            raise RuntimeError("x")
        return "ok"

    register(_task("a", run=body))
    run_pending()
    state["fail"] = False
    assert run_pending()[0]["outcome"] == "succeeded"
    assert PostDeployTaskRun.objects.get(task_id="a").attempts == 2


@pytest.mark.django_db
def test_skipped_is_reevaluated_next_run() -> None:
    reason: dict[str, str | None] = {"value": "not ready"}
    register(_task("a", skip_reason=lambda: reason["value"]))
    assert run_pending() == [{"id": "a", "outcome": "skipped", "detail": "not ready"}]
    row = PostDeployTaskRun.objects.get(task_id="a")
    assert (row.status, row.output, row.attempts) == (Status.SKIPPED, "not ready", 0)
    reason["value"] = None
    assert run_pending()[0]["outcome"] == "succeeded"


@pytest.mark.django_db
def test_dry_run_writes_nothing() -> None:
    register(_task("a", "1.0.0"))
    register(_task("b", "1.0.1", skip_reason=lambda: "nope"))
    results = run_pending(dry_run=True)
    assert [(r["id"], r["outcome"]) for r in results] == [("a", "would_run"), ("b", "skipped")]
    assert PostDeployTaskRun.objects.count() == 0


@pytest.mark.django_db
def test_output_is_truncated() -> None:
    register(_task("a", run=lambda: "x" * 10_000))
    run_pending()
    assert len(PostDeployTaskRun.objects.get(task_id="a").output) == post_deploy.MAX_OUTPUT_LENGTH


@pytest.mark.django_db(transaction=True)
def test_busy_when_row_locked_by_another_connection() -> None:
    """Use ``transaction=True``: the lock must be held by a second, really committed connection.

    The default test transaction would hide the row from the other thread's connection.
    """
    register(_task("a"))
    row = PostDeployTaskRun.objects.create(task_id="a", release="1.0.0")
    locked, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        try:
            with transaction.atomic():
                PostDeployTaskRun.objects.select_for_update().get(pk=row.pk)
                locked.set()
                release.wait(timeout=10)
        finally:
            connection.close()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    try:
        assert locked.wait(timeout=10)
        assert run_pending()[0]["outcome"] == "busy"
    finally:
        release.set()
        thread.join(timeout=10)
    assert run_pending()[0]["outcome"] == "succeeded"


def _raise() -> str | None:
    raise RuntimeError("skip check broke")


@pytest.mark.django_db
def test_skip_reason_exception_is_recorded_as_failed_and_isolated() -> None:
    register(_task("a", "1.0.0", skip_reason=_raise))
    register(_task("b", "1.0.1"))
    results = run_pending()
    assert [r["outcome"] for r in results] == ["failed", "succeeded"]
    row = PostDeployTaskRun.objects.get(task_id="a")
    assert (row.status, row.attempts, row.output) == (Status.FAILED, 1, "RuntimeError: skip check broke")
    assert row.finished_at is not None


@pytest.mark.django_db
def test_dry_run_skip_reason_exception_is_reported_and_writes_nothing() -> None:
    register(_task("a", "1.0.0", skip_reason=_raise))
    register(_task("b", "1.0.1"))
    results = run_pending(dry_run=True)
    assert results[0] == {"id": "a", "outcome": "failed", "detail": "RuntimeError: skip check broke"}
    assert results[1]["outcome"] == "would_run"
    assert PostDeployTaskRun.objects.count() == 0


@pytest.mark.django_db
def test_skip_clears_stale_started_at() -> None:
    state: dict[str, str | None] = {"reason": None}

    def body() -> str:
        raise RuntimeError("x")

    register(_task("a", run=body, skip_reason=lambda: state["reason"]))
    run_pending()
    assert PostDeployTaskRun.objects.get(task_id="a").started_at is not None
    state["reason"] = "not now"
    run_pending()
    row = PostDeployTaskRun.objects.get(task_id="a")
    assert row.status == Status.SKIPPED
    assert row.started_at is None


def _broken_sql() -> str | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1/0")
    return None


@pytest.mark.django_db
def test_skip_reason_database_error_is_isolated_in_a_savepoint() -> None:
    register(_task("a", "1.0.0", skip_reason=_broken_sql))
    register(_task("b", "1.0.1"))
    results = run_pending()
    assert [r["outcome"] for r in results] == ["failed", "succeeded"]
    assert PostDeployTaskRun.objects.get(task_id="a").status == Status.FAILED
    assert PostDeployTaskRun.objects.get(task_id="b").status == Status.SUCCEEDED
