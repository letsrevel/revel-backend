#!/usr/bin/env python3
"""Query production logs from Loki through the Grafana datasource-proxy API.

Loki is not exposed publicly; Grafana (https://grafana.letsrevel.io) is. Grafana
proxies authenticated requests to its Loki datasource, so this tool talks to
Grafana with a service-account token and asks it to run LogQL against Loki.

Auth: a Grafana service-account token (role *Viewer* is enough), read via
``python-decouple`` from ``GRAFANA_TOKEN`` (env var or the project ``.env``).
``GRAFANA_URL`` and ``GRAFANA_LOKI_UID`` are optional overrides with sane defaults.

Log shape (structlog JSON, parsed by Alloy before ingestion):
  * stream labels:        ``service_name`` (web|celery_default|beat|telegram),
                          ``level`` (info|warning|error|...), ``environment``
  * structured metadata:  ``trace_id``, ``request_id``, ``method``, ``path``,
                          ``status_code``, ``user_id``, ``ip_address``, ``user_agent``

Run with the venv interpreter (imports ``python-decouple``). Examples::

    # last hour of web errors
    .venv/bin/python scripts/loki_logs.py web --level error

    # everything for one request, across services, last 6h
    .venv/bin/python scripts/loki_logs.py --request-id 9f3c... --since 6h --forward

    # celery failures mentioning "Timeout" in the last day, newest first
    .venv/bin/python scripts/loki_logs.py celery_default --since 1d --grep Timeout

    # 500s in the API, last 2h
    .venv/bin/python scripts/loki_logs.py web --status-code 500 --since 2h

    # raw LogQL escape hatch
    .venv/bin/python scripts/loki_logs.py -q '{service_name="web"} | method=`POST`' --since 2h

    # discover what's available
    .venv/bin/python scripts/loki_logs.py --labels
    .venv/bin/python scripts/loki_logs.py --label-values service_name
"""
# ruff: noqa: T201  -- this is a CLI; printing to stdout is the whole point.

from __future__ import annotations

import argparse
import json
import sys
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request

from decouple import config

DEFAULT_GRAFANA_URL = "https://grafana.letsrevel.io"
DEFAULT_DATASOURCE_UID = "loki"
USER_AGENT = "revel-loki-logs/1.0 (+scripts/loki_logs.py)"
KNOWN_SERVICES = ("web", "celery_default", "beat", "telegram")
# Per-record fields we extract for display beneath each log line.
METADATA_FIELDS = ("trace_id", "request_id", "method", "path", "status_code", "user_id", "ip_address", "user_agent")
# Order in which metadata is shown beneath each log line (most useful first).
META_DISPLAY_ORDER = ("status_code", "method", "path", "user_id", "ip_address", "user_agent", "request_id", "trace_id")
# Fields the live pipeline promotes to structured metadata → queryable as
# ``| field="x"`` (since the single-render structlog fix, v1.62.4, all are
# promoted — ip_address/user_agent only on lines ingested after the matching
# alloy-config update; older lines simply won't match those filters).
LABEL_METADATA = ("trace_id", "request_id", "method", "path", "status_code", "user_id", "ip_address", "user_agent")
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class LokiError(RuntimeError):
    """Raised for any user-facing failure (missing token, HTTP error, bad args)."""


# --------------------------------------------------------------------------- #
# Auth / connection
# --------------------------------------------------------------------------- #
def resolve_token() -> str:
    """Read the Grafana service-account token from GRAFANA_TOKEN (env or .env)."""
    token = str(config("GRAFANA_TOKEN", default="")).strip()
    if not token:
        raise LokiError(
            "GRAFANA_TOKEN is not set. Add it to your .env (or environment).\n"
            "Create one in Grafana → Administration → Service accounts → add a "
            "'Viewer' account → Add token, then put it in .env as:\n"
            "  GRAFANA_TOKEN=<token>"
        )
    return token


def loki_api_base(grafana_url: str, uid: str) -> str:
    """Build the Grafana proxy prefix that fronts Loki's HTTP API."""
    return f"{grafana_url.rstrip('/')}/api/datasources/proxy/uid/{uid}/loki/api/v1"


def http_get(url: str, token: str, params: dict[str, t.Any]) -> dict[str, t.Any]:
    """GET ``url`` with a Bearer token and return the decoded JSON body."""
    query = urllib.parse.urlencode(params, doseq=True)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={
            "Authorization": f"Bearer {token}",
            # Cloudflare fronts grafana.letsrevel.io and bans the default
            # Python-urllib UA (error 1010); send a normal browser-ish one.
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return t.cast("dict[str, t.Any]", json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        hint = ""
        if exc.code in (401, 403):
            hint = " (token missing/expired, lacks Viewer access, or blocked by Cloudflare)"
        elif exc.code == 404:
            hint = " (wrong GRAFANA_URL or GRAFANA_LOKI_UID? uid defaults to 'loki')"
        raise LokiError(f"HTTP {exc.code} from Grafana{hint}: {body}") from exc
    except urllib.error.URLError as exc:
        raise LokiError(f"Could not reach Grafana at {url}: {exc.reason}") from exc


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def parse_duration(value: str) -> int:
    """Parse a duration like ``90m``, ``2h``, ``1d`` into seconds."""
    value = value.strip().lower()
    unit = value[-1:]
    if unit not in DURATION_UNITS or not value[:-1].isdigit():
        raise LokiError(f"Invalid duration {value!r}. Use e.g. 30s, 45m, 2h, 1d, 1w.")
    return int(value[:-1]) * DURATION_UNITS[unit]


def to_nanos(value: str) -> int:
    """Convert an RFC3339 string or unix-seconds string to unix nanoseconds."""
    value = value.strip()
    if value.isdigit():
        # Treat <1e12 as seconds, otherwise assume already finer-grained.
        seconds = int(value)
        return seconds * 1_000_000_000 if seconds < 1_000_000_000_000 else seconds
    import datetime as dt

    text = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return int(parsed.timestamp() * 1_000_000_000)


def resolve_window(args: argparse.Namespace) -> tuple[int, int]:
    """Return (start_ns, end_ns) from --from/--to or --since (default 1h)."""
    now_ns = int(time.time() * 1_000_000_000)
    end_ns = to_nanos(args.to) if args.to else now_ns
    if args.from_:
        return to_nanos(args.from_), end_ns
    return end_ns - parse_duration(args.since) * 1_000_000_000, end_ns


# --------------------------------------------------------------------------- #
# LogQL construction
# --------------------------------------------------------------------------- #
def _logql_string(value: str) -> str:
    """Quote a value for LogQL, preferring backticks to dodge escaping."""
    if "`" not in value:
        return f"`{value}`"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(args: argparse.Namespace) -> str:  # noqa: C901
    """Assemble a LogQL query from the structured flags, or pass --query through."""
    if args.query:
        return args.query

    selectors: list[str] = []
    if args.service:
        selectors.append(f"service_name={_logql_string(args.service)}")
    if args.env:
        selectors.append(f"environment={_logql_string(args.env)}")
    if args.level:
        selectors.append(f"level=~{_logql_string('|'.join(args.level))}")
    if not selectors:
        # Loki requires at least one matcher; service_name is on every app stream.
        selectors.append('service_name=~".+"')
    query = "{" + ", ".join(selectors) + "}"

    for needle in args.grep or []:
        query += f" |= {_logql_string(needle)}"
    for needle in args.exclude or []:
        query += f" != {_logql_string(needle)}"
    for pattern in args.regex or []:
        query += f" |~ {_logql_string(pattern)}"

    # All request-metadata fields are promoted to structured metadata → label filters.
    for field in LABEL_METADATA:
        val = getattr(args, field)
        if val is not None:
            query += f" | {field}={_logql_string(val)}"
    return query


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def _fmt_ts(nanos: str) -> str:
    """Render a unix-nanosecond string as local ``HH:MM:SS.mmm`` (with date if old)."""
    import datetime as dt

    moment = dt.datetime.fromtimestamp(int(nanos) / 1_000_000_000).astimezone()
    today = dt.datetime.now().astimezone().date()
    fmt = "%H:%M:%S.%f" if moment.date() == today else "%Y-%m-%d %H:%M:%S.%f"
    return moment.strftime(fmt)[:-3]


def _flatten_entries(result: list[dict[str, t.Any]]) -> list[tuple[str, dict[str, str], str]]:
    """Merge all streams into a single (ts, labels, line) list sorted oldest→newest."""
    entries: list[tuple[str, dict[str, str], str]] = []
    for stream in result:
        labels = stream.get("stream", {})
        for value in stream.get("values", []):
            entries.append((value[0], labels, value[1]))
    entries.sort(key=lambda item: int(item[0]))
    return entries


def _render_entry(labels: dict[str, str], line: str) -> tuple[str, str, dict[str, str]]:
    """Return (level, message, metadata) for one entry.

    structlog logs the whole record as a JSON object; the human-readable bit is the
    ``event`` field. Celery also prepends a ``[ts: LEVEL/Worker]`` banner before that
    JSON, so we extract the first ``{...}`` rather than requiring a pure-JSON line.
    Parsed values (level, metadata) win over the stream labels, which can be stale or
    missing fields the live ingestion pipeline didn't promote.
    """
    level = labels.get("level", "-")
    message = line
    meta = {k: v for k, v in labels.items() if k in METADATA_FIELDS and v}
    start, end = line.find("{"), line.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(line[start : end + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            message = str(obj.get("event", line))
            if obj.get("level"):
                level = str(obj["level"])
            for field in METADATA_FIELDS:
                if obj.get(field) not in (None, "") and field not in meta:
                    meta[field] = str(obj[field])
    return level, message, meta


def print_entries(result: list[dict[str, t.Any]], show_meta: bool) -> int:
    """Pretty-print log lines; return the count printed."""
    entries = _flatten_entries(result)
    for nanos, labels, line in entries:
        service = labels.get("service_name", "-")
        level, message, meta = _render_entry(labels, line)
        print(f"{_fmt_ts(nanos)}  {level.upper():<7} {service:<14} {message}")
        if show_meta:
            ordered = [f"{k}={meta[k]}" for k in META_DISPLAY_ORDER if k in meta]
            if ordered:
                print(f"{'':>13}  {'  '.join(ordered)}")
    return len(entries)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def run_discovery(args: argparse.Namespace, base: str, token: str) -> int:
    """Handle --labels / --label-values without running a log query."""
    if args.labels:
        data = http_get(f"{base}/labels", token, {})
        for name in data.get("data", []):
            print(name)
        return 0
    data = http_get(f"{base}/label/{urllib.parse.quote(args.label_values)}/values", token, {})
    for value in data.get("data", []):
        print(value)
    return 0


def run_query(args: argparse.Namespace, base: str, token: str) -> int:
    """Run a range query and render the results."""
    query = build_query(args)
    start_ns, end_ns = resolve_window(args)
    params = {
        "query": query,
        "start": start_ns,
        "end": end_ns,
        "limit": args.limit,
        "direction": "forward" if args.forward else "backward",
    }
    if args.print_query:
        print(f"LogQL : {query}")
        print(f"start : {start_ns}")
        print(f"end   : {end_ns}")
        print(f"limit : {args.limit}")
        return 0

    data = http_get(f"{base}/query_range", token, params)
    result = data.get("data", {}).get("result", [])
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    count = print_entries(result, show_meta=not args.no_meta)
    if count == 0:
        print(f"(no log lines for: {query})", file=sys.stderr)
    elif count >= args.limit:
        print(f"\n(hit --limit {args.limit}; narrow the window or raise --limit for more)", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="loki_logs.py",
        description="Query production logs from Loki via the Grafana proxy API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "service",
        nargs="?",
        help=f"service_name filter (one of: {', '.join(KNOWN_SERVICES)})",
    )

    flt = parser.add_argument_group("filters")
    flt.add_argument("-q", "--query", help="raw LogQL (overrides all filter flags below)")
    flt.add_argument("-g", "--grep", action="append", metavar="TEXT", help="line must contain TEXT (repeatable, AND)")
    flt.add_argument("-x", "--exclude", action="append", metavar="TEXT", help="line must NOT contain TEXT (repeatable)")
    flt.add_argument("-r", "--regex", action="append", metavar="RE", help="line matches regex RE (repeatable)")
    flt.add_argument("-l", "--level", action="append", metavar="LVL", help="level label, e.g. error (repeatable → OR)")
    flt.add_argument("--env", metavar="ENV", help="environment label, e.g. production")
    flt.add_argument("--trace-id", dest="trace_id", help="structured-metadata trace_id")
    flt.add_argument("--request-id", dest="request_id", help="structured-metadata request_id")
    flt.add_argument("--user-id", dest="user_id", help="structured-metadata user_id")
    flt.add_argument("--method", help="HTTP method metadata, e.g. POST")
    flt.add_argument("--path", help="request path metadata (exact match)")
    flt.add_argument("--status-code", dest="status_code", help="HTTP status_code metadata, e.g. 500")
    flt.add_argument("--ip", dest="ip_address", help="client ip_address metadata (exact match)")
    flt.add_argument("--user-agent", dest="user_agent", help="user_agent metadata (exact match)")

    win = parser.add_argument_group("time & limit")
    win.add_argument("-s", "--since", default="1h", metavar="DUR", help="look back this far (default 1h): 30m,2h,1d,1w")
    win.add_argument(
        "--from", dest="from_", metavar="TS", help="start time (RFC3339 or unix seconds); overrides --since"
    )
    win.add_argument("--to", metavar="TS", help="end time (RFC3339 or unix seconds); default now")
    win.add_argument("-n", "--limit", type=int, default=100, metavar="N", help="max lines (default 100)")
    win.add_argument("--forward", action="store_true", help="oldest first (default newest first)")

    out = parser.add_argument_group("output")
    out.add_argument("--json", action="store_true", help="print raw Loki JSON response")
    out.add_argument("--no-meta", action="store_true", help="hide the structured-metadata line")
    out.add_argument("--print-query", action="store_true", help="print the built LogQL and exit (no request)")

    disc = parser.add_argument_group("discovery")
    disc.add_argument("--labels", action="store_true", help="list available label names and exit")
    disc.add_argument("--label-values", metavar="LABEL", help="list values for LABEL and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    try:
        token = resolve_token()
        grafana_url = str(config("GRAFANA_URL", default=DEFAULT_GRAFANA_URL))
        uid = str(config("GRAFANA_LOKI_UID", default=DEFAULT_DATASOURCE_UID))
        base = loki_api_base(grafana_url, uid)
        if args.labels or args.label_values:
            return run_discovery(args, base, token)
        return run_query(args, base, token)
    except LokiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
