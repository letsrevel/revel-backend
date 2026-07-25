"""Shared plumbing for the seating load tests: Django bootstrap, JWTs, HTTP client, stats.

The load itself is pure HTTP (httpx against 127.0.0.1:8000); the ORM is only used
from the main thread for fixture selection, JWT minting and post-run invariant checks.
"""

import dataclasses
import os
import statistics
import sys
import threading
import time
import typing as t
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# Log lines that are pre-existing environment/seed-data noise, not load-test failures:
# - seeded users have reserved-TLD emails like @bootstrap.example, which the
#   notification pipeline's EmailStr validation rejects on every ticket_created;
# - seeded users carry fake Telegram chat ids, so eager-Celery telegram sends fail
#   ("chat not found") and aiogram leaks an "Unclosed client session" complaint.
KNOWN_LOG_NOISE = (
    "notification_render_failed",
    "email_notification_failed",
    "Error sending message to Telegram ID",
    "send_message_task",
    "Unclosed client session",
    "Unclosed connector",
)


def setup_django() -> None:
    """Configure Django so the harness can use the ORM from the repo root."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "revel.settings")
    import django

    django.setup()


def mint_tokens(users: t.Sequence[t.Any]) -> list[str]:
    """Mint a JWT access token per user via the project's ninja_jwt machinery."""
    from ninja_jwt.tokens import RefreshToken

    return [str(RefreshToken.for_user(u).access_token) for u in users]


def pick_users(offset: int, count: int) -> list[t.Any]:
    """Deterministic slice of plain seeded users (active, non-staff, non-superuser)."""
    from accounts.models import RevelUser

    users = list(
        RevelUser.objects.filter(is_active=True, is_staff=False, is_superuser=False).order_by("id")[
            offset : offset + count
        ]
    )
    if len(users) != count:
        raise RuntimeError(f"Need {count} users at offset {offset}, found {len(users)}")
    return users


@dataclasses.dataclass
class Call:
    """One HTTP call result."""

    label: str
    status: int
    elapsed_ms: float
    body_size: int
    body: t.Any = None


class LoadClient:
    """Thread-safe httpx wrapper that records status/latency/body per call."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        """Create the pooled client (sized for ~120 concurrent workers)."""
        limits = httpx.Limits(max_connections=150, max_keepalive_connections=150)
        self._client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(60.0), limits=limits)
        self._lock = threading.Lock()
        self.calls: list[Call] = []

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        json_body: t.Any | None = None,
        label: str = "",
    ) -> Call:
        """Fire one request, parse JSON if possible, and record the call."""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        start = time.perf_counter()
        try:
            try:
                resp = self._client.request(method, path, headers=headers, json=json_body)
            except httpx.TransportError:
                if method != "GET":
                    raise
                # One retry for idempotent GETs: gunicorn --max-requests recycling can
                # reset a pooled keep-alive connection mid-flight; any real client retries.
                resp = self._client.request(method, path, headers=headers, json=json_body)
            elapsed = (time.perf_counter() - start) * 1000
            try:
                body = resp.json()
            except ValueError:
                body = None
            call = Call(label, resp.status_code, elapsed, len(resp.content), body)
        except httpx.HTTPError as exc:
            elapsed = (time.perf_counter() - start) * 1000
            call = Call(label, -1, elapsed, 0, {"transport_error": str(exc)})
        with self._lock:
            self.calls.append(call)
        return call

    def take_calls(self) -> list[Call]:
        """Return and clear the recorded calls (one scenario's worth)."""
        with self._lock:
            calls, self.calls = self.calls, []
        return calls


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered)) - 1))
    return ordered[idx]


def status_counts(calls: list[Call]) -> dict[str, int]:
    """Bucket calls into 2xx/4xx/5xx/transport-error counts."""
    buckets = {"2xx": 0, "409": 0, "other_4xx": 0, "5xx": 0, "transport": 0}
    for c in calls:
        if c.status == -1:
            buckets["transport"] += 1
        elif 200 <= c.status < 300:
            buckets["2xx"] += 1
        elif c.status == 409:
            buckets["409"] += 1
        elif 400 <= c.status < 500:
            buckets["other_4xx"] += 1
        elif c.status >= 500:
            buckets["5xx"] += 1
    return buckets


def print_latency_block(name: str, calls: list[Call], wall_s: float) -> None:
    """Print RPS, latency percentiles and body-size stats for a group of calls."""
    lat = [c.elapsed_ms for c in calls]
    sizes = [c.body_size for c in calls]
    counts = status_counts(calls)
    rps = len(calls) / wall_s if wall_s > 0 else 0.0
    print(f"  [{name}] n={len(calls)} wall={wall_s:.2f}s rps={rps:.1f} statuses={counts}")
    if lat:
        print(
            f"    latency ms: p50={percentile(lat, 50):.1f} p95={percentile(lat, 95):.1f} "
            f"p99={percentile(lat, 99):.1f} max={max(lat):.1f} mean={statistics.mean(lat):.1f}"
        )
    if sizes:
        print(f"    body bytes: mean={int(statistics.mean(sizes))} max={max(sizes)}")


class LogWatcher:
    """Scans the gunicorn log for new tracebacks / error records since the last mark."""

    def __init__(self, path: str) -> None:
        """Remember the log path and start scanning from its current end."""
        self.path = Path(path)
        self._offset = self.path.stat().st_size if self.path.exists() else 0

    def scan(self) -> tuple[list[str], list[str]]:
        """Return (real error lines, known-noise lines) appended since the last scan."""
        if not self.path.exists():
            return [], []
        with self.path.open("r", errors="replace") as fh:
            fh.seek(self._offset)
            new = fh.read()
            self._offset = fh.tell()
        errors: list[str] = []
        noise: list[str] = []
        for line in new.splitlines():
            interesting = "Traceback" in line or '"level": "error"' in line or "Internal Server Error" in line
            if not interesting:
                continue
            if any(marker in line for marker in KNOWN_LOG_NOISE):
                noise.append(line)
            else:
                errors.append(line)
        return errors, noise

    def report(self, scenario: str) -> list[str]:
        """Print a one-line summary of new log errors for a scenario; return the error lines."""
        errors, noise = self.scan()
        if errors:
            print(f"  [gunicorn log] {scenario}: {len(errors)} NEW ERROR line(s) (see report)")
            for line in errors[:3]:
                print(f"    {line[:220]}")
        else:
            print(f"  [gunicorn log] {scenario}: no new tracebacks/errors")
        if noise:
            print(f"  [gunicorn log] {scenario}: {len(noise)} known seed-data notification noise line(s) (ignored)")
        return errors


@dataclasses.dataclass
class ScenarioResult:
    """Outcome of one scenario: pass/fail plus messages for the final summary."""

    name: str
    passed: bool
    notes: list[str] = dataclasses.field(default_factory=list)


def check(condition: bool, ok_msg: str, fail_msg: str, notes: list[str]) -> bool:
    """Print and record a single assertion outcome; return the condition."""
    if condition:
        print(f"  PASS: {ok_msg}")
        notes.append(f"PASS: {ok_msg}")
    else:
        print(f"  FAIL: {fail_msg}")
        notes.append(f"FAIL: {fail_msg}")
    return condition
