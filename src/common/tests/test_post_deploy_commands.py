"""Tests for the ``run_post_deploy`` and ``post_deploy_status`` commands (#1508)."""

import typing as t
from collections.abc import Iterator
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from common import post_deploy
from common.models import PostDeployTaskRun
from common.post_deploy import PostDeployTask, register

Status = PostDeployTaskRun.Status


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, PostDeployTask]]:
    """Swap the module registry for an empty one so the real tasks do not interfere."""
    fresh: dict[str, PostDeployTask] = {}
    monkeypatch.setattr(post_deploy, "_REGISTRY", fresh)
    yield fresh


def _register(task_id: str, release: str = "1.0.0", **kwargs: t.Any) -> None:
    kwargs.setdefault("run", lambda: "ok")
    register(PostDeployTask(id=task_id, release=release, description="d", **kwargs))


def _call(command: str, *args: str) -> str:
    out = StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_run_disabled_flag_runs_nothing(settings: t.Any) -> None:
    settings.FEATURE_POST_DEPLOY_TASKS = False
    _register("a")
    assert "disabled (FEATURE_POST_DEPLOY_TASKS=False); nothing ran" in _call("run_post_deploy")
    assert PostDeployTaskRun.objects.count() == 0


@pytest.mark.django_db
def test_run_enabled_flag_runs(settings: t.Any) -> None:
    settings.FEATURE_POST_DEPLOY_TASKS = True
    _register("a")
    assert "a: succeeded" in _call("run_post_deploy")
    assert PostDeployTaskRun.objects.get(task_id="a").status == Status.SUCCEEDED


@pytest.mark.django_db
def test_run_dry_run_works_with_flag_off(settings: t.Any) -> None:
    settings.FEATURE_POST_DEPLOY_TASKS = False
    _register("a")
    assert "a: would_run" in _call("run_post_deploy", "--dry-run")
    assert PostDeployTaskRun.objects.count() == 0


@pytest.mark.django_db
def test_run_failure_exits_zero(settings: t.Any) -> None:
    settings.FEATURE_POST_DEPLOY_TASKS = True

    def boom() -> str:
        raise RuntimeError("x")

    _register("a", run=boom)
    assert "a: failed" in _call("run_post_deploy")


@pytest.mark.django_db
def test_status_lists_pending_and_unregistered() -> None:
    _register("a")
    _register("b", "1.0.1")
    PostDeployTaskRun.objects.create(task_id="a", release="1.0.0", status=Status.SUCCEEDED, attempts=1)
    PostDeployTaskRun.objects.create(task_id="old", release="0.1.0", status=Status.SUCCEEDED)
    lines = {line.split()[0]: line for line in _call("post_deploy_status").splitlines()[1:]}
    assert "succeeded" in lines["a"]
    assert "pending" in lines["b"]
    assert "unregistered" in lines["old"]


@pytest.mark.django_db
@pytest.mark.parametrize("status", [Status.FAILED, None])
def test_status_check_fails_on_failed_or_pending(status: str | None) -> None:
    _register("a")
    if status:
        PostDeployTaskRun.objects.create(task_id="a", release="1.0.0", status=status, finished_at=timezone.now())
    with pytest.raises(CommandError):
        _call("post_deploy_status", "--check")


@pytest.mark.django_db
def test_status_shows_unfinished_row_as_running_and_check_fails() -> None:
    _register("a")
    PostDeployTaskRun.objects.create(task_id="a", release="1.0.0", status=Status.FAILED, attempts=1)
    lines = {line.split()[0]: line for line in _call("post_deploy_status").splitlines()[1:]}
    assert "running" in lines["a"]
    assert "failed" not in lines["a"].lower()
    with pytest.raises(CommandError, match=r"a \(running\)"):
        _call("post_deploy_status", "--check")


@pytest.mark.django_db
def test_status_check_passes_on_succeeded_and_skipped() -> None:
    _register("a")
    _register("b", "1.0.1")
    PostDeployTaskRun.objects.create(task_id="a", release="1.0.0", status=Status.SUCCEEDED)
    PostDeployTaskRun.objects.create(task_id="b", release="1.0.1", status=Status.SKIPPED)
    _call("post_deploy_status", "--check")
