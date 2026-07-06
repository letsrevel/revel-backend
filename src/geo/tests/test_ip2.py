"""Thread-safety tests for the IP2Location handle cache (issue #637).

FILE_IO mode reads through a single shared file cursor, so one instance shared
across gthread worker threads corrupts concurrent reads. ``get_ip2location`` must
hand each thread its own handle, while still hot-reloading when the ``.BIN`` is
replaced by the downloader.
"""

import threading
import typing as t
from unittest.mock import MagicMock

import pytest
from pytest import MonkeyPatch

from geo.ip2 import get_ip2location


@pytest.fixture(autouse=True)
def mock_ip2location() -> t.Iterator[None]:
    """Shadow conftest's autouse mock so these tests hit the real get_ip2location."""
    yield


def test_get_ip2location_is_thread_local(monkeypatch: MonkeyPatch) -> None:
    """Each thread gets its own handle; a thread reuses its own across calls."""
    calls = {"n": 0}
    calls_lock = threading.Lock()

    class FakeDB:
        def __init__(self, path: t.Any) -> None:
            with calls_lock:
                calls["n"] += 1

    monkeypatch.setattr("geo.ip2.IP2Location", FakeDB)

    n_threads = 5
    barrier = threading.Barrier(n_threads)
    results: dict[int, tuple[t.Any, t.Any]] = {}
    results_lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()  # release all threads together to force concurrent access
        first = get_ip2location()
        second = get_ip2location()
        with results_lock:
            results[i] = (first, second)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # One construction per thread — no shared handle, no redundant re-loads.
    assert calls["n"] == n_threads
    # Within a thread the same handle is reused...
    for first, second in results.values():
        assert first is second
    # ...and every thread received a distinct handle.
    handles = [first for first, _ in results.values()]
    assert len({id(h) for h in handles}) == n_threads


def test_get_ip2location_reloads_on_mtime_change(monkeypatch: MonkeyPatch) -> None:
    """A thread reloads its handle when the database file's mtime changes."""
    calls = {"n": 0}

    class FakeDB:
        def __init__(self, path: t.Any) -> None:
            calls["n"] += 1

    monkeypatch.setattr("geo.ip2.IP2Location", FakeDB)

    fake_path = MagicMock()
    fake_path.stat.return_value.st_mtime = 1.0
    monkeypatch.setattr("geo.ip2.conf.IP2LOCATION_DB_PATH", fake_path)

    errors: list[Exception] = []

    def run() -> None:
        try:
            db1 = get_ip2location()
            db2 = get_ip2location()  # same mtime -> no reload
            assert db1 is db2
            assert calls["n"] == 1

            fake_path.stat.return_value.st_mtime = 2.0  # downloader replaced the .BIN
            db3 = get_ip2location()  # different mtime -> reload
            assert db3 is not db1
            assert calls["n"] == 2
        except Exception as e:  # surface assertion failures from the worker thread
            errors.append(e)

    # Run on a fresh thread so thread-local state starts empty.
    th = threading.Thread(target=run)
    th.start()
    th.join()

    assert not errors, errors
