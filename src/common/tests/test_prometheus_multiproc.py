"""Tests for Prometheus multiprocess support (#757).

Two halves, both gated on the ``PROMETHEUS_MULTIPROC_DIR`` env var:

- The ``/metrics`` view behaviour we depend on from the installed libraries,
  pinned here so an upgrade that breaks it fails loudly: with the env var
  unset the default in-process registry is served (today's behaviour); with
  it set, ``django_prometheus.exports.ExportToDjangoView`` serves a fresh
  registry backed by ``prometheus_client.multiprocess.MultiProcessCollector``,
  aggregating the mmap files that every worker process writes.
- The gunicorn hooks in ``src/gunicorn.conf.py`` (auto-loaded by gunicorn from
  its working directory): wipe stale files when the master starts, clean up
  live-gauge files when a worker dies. Both must be no-ops when the env var
  is unset.
"""

import importlib.util
import os
import subprocess
import sys
import types
import typing as t
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[2]
GUNICORN_CONF = SRC_DIR / "gunicorn.conf.py"

# A minimal script for a child process: import prometheus_client under
# PROMETHEUS_MULTIPROC_DIR (so ValueClass resolves to MmapedValue at import
# time, exactly as in a gunicorn worker) and increment a counter.
_CHILD_SCRIPT = """
import sys
from prometheus_client import Counter

Counter("demo_multiproc", "Cross-process demo counter").inc(float(sys.argv[1]))
"""


def _load_gunicorn_conf() -> types.ModuleType:
    """Load src/gunicorn.conf.py as a module (its name is not importable)."""
    spec = importlib.util.spec_from_file_location("gunicorn_conf", GUNICORN_CONF)
    assert spec is not None and spec.loader is not None, f"missing {GUNICORN_CONF}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _increment_counter_in_subprocess(multiproc_dir: Path, amount: float) -> None:
    """Increment the demo counter from a separate process (its own pid file)."""
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(multiproc_dir)}
    subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CHILD_SCRIPT, str(amount)],
        env=env,
        check=True,
        capture_output=True,
    )


class _FakeWorker:
    def __init__(self, pid: int) -> None:
        self.pid = pid


# ---------------------------------------------------------------------------
# /metrics view behaviour
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_metrics_view_serves_default_registry_without_env_var(client: t.Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env var: today's behaviour — the in-process default registry."""
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.content
    # Registered on the default in-process registry at import time.
    assert b"revel_stripe_session_total_mismatch" in body
    assert b"demo_multiproc_total" not in body


@pytest.mark.django_db
def test_metrics_view_serves_multiprocess_registry_with_env_var(
    client: t.Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Set env var: the view aggregates mmap files from all worker processes."""
    _increment_counter_in_subprocess(tmp_path, 3)
    _increment_counter_in_subprocess(tmp_path, 4)
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))

    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.content
    # Values written by two distinct processes are summed in one scrape.
    assert b"demo_multiproc_total 7.0" in body
    # And it is the multiprocess registry, not the in-process default one:
    # platform/process collectors only live on the default registry.
    assert b"python_info" not in body


# ---------------------------------------------------------------------------
# gunicorn.conf.py hooks
# ---------------------------------------------------------------------------


def test_hooks_are_noops_without_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without the env var both hooks return without touching anything."""
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    conf = _load_gunicorn_conf()
    stray = tmp_path / "counter_1.db"
    stray.touch()
    conf.on_starting(None)
    conf.child_exit(None, _FakeWorker(pid=1))
    assert stray.exists()


def test_on_starting_wipes_stale_metric_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Master start wipes leftover *.db files from a previous run."""
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    stale = [tmp_path / "counter_123.db", tmp_path / "gauge_all_456.db"]
    for f in stale:
        f.touch()
    unrelated = tmp_path / "not-a-metric.txt"
    unrelated.touch()

    conf = _load_gunicorn_conf()
    conf.on_starting(None)

    assert not any(f.exists() for f in stale)
    assert unrelated.exists()


def test_child_exit_removes_dead_workers_live_gauge_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Worker death removes its live-gauge files; counter files must survive."""
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    live = [tmp_path / "gauge_livesum_123.db", tmp_path / "gauge_liveall_123.db"]
    for f in live:
        f.touch()
    keep = [
        tmp_path / "counter_123.db",  # counters are monotonic totals
        tmp_path / "gauge_livesum_999.db",  # other, still-alive worker
    ]
    for f in keep:
        f.touch()

    conf = _load_gunicorn_conf()
    conf.child_exit(None, _FakeWorker(pid=123))

    assert not any(f.exists() for f in live)
    assert all(f.exists() for f in keep)
