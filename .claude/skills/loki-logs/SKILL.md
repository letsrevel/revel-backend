---
name: loki-logs
description: Use when investigating production behaviour from logs — a 500/error in prod, a failing or stuck Celery task, tracing one request/trace_id/user across services, or confirming a deploy's runtime effect. Pulls real Loki logs from the live stack (web, celery_default, beat, telegram) via Grafana.
allowed-tools: Bash(python:*), Bash(.venv/bin/python:*), Read, Grep
---

# Loki Logs

Query the **production** logs from Loki, the live log store. Loki itself is not
publicly reachable; the script asks Grafana (`grafana.letsrevel.io`) to proxy
LogQL to its Loki datasource using a service-account token.

## Tool

```bash
.venv/bin/python scripts/loki_logs.py [SERVICE] [filters] [--since DUR] [-n LIMIT]
```

`SERVICE` is one of `web`, `celery_default`, `beat`, `telegram` (omit to search all).
Default window is the **last 1h**, newest first, 100 lines. Run `--help` for the full flag list.
Use the venv interpreter — the script imports `python-decouple`.

**Auth:** reads `GRAFANA_TOKEN` via decouple (from the project `.env` or the environment).
If it errors with "GRAFANA_TOKEN is not set", it isn't configured yet — tell the user; do not guess.

## What you can filter on

| Kind | Flags | LogQL |
|------|-------|-------|
| Stream labels | `SERVICE`, `--level error` (repeatable), `--env production` | indexed, cheap |
| Line content | `--grep TEXT`, `--exclude TEXT`, `--regex RE` (all repeatable) | substring / regex |
| Request metadata | `--trace-id`, `--request-id`, `--user-id`, `--method`, `--path`, `--status-code` | structlog fields |
| Raw escape hatch | `--query '{service_name="web"} \| status_code="500"'` | any LogQL |

Logs are structlog JSON; the displayed line is the `event` message, with a
metadata line beneath it (suppress with `--no-meta`, get raw JSON with `--json`).

## Recipes

```bash
# Errors in the API in the last hour
.venv/bin/python scripts/loki_logs.py web --level error

# Follow one request end-to-end across every service
.venv/bin/python scripts/loki_logs.py --request-id <uuid> --since 6h --forward

# Celery failures mentioning a traceback, last day
.venv/bin/python scripts/loki_logs.py celery_default --since 1d --grep Traceback

# All 5xx responses, last 2h
.venv/bin/python scripts/loki_logs.py web --status-code 500 --since 2h

# Everything a user triggered
.venv/bin/python scripts/loki_logs.py --user-id <uuid> --since 12h

# Discover what labels/values exist
.venv/bin/python scripts/loki_logs.py --labels
.venv/bin/python scripts/loki_logs.py --label-values service_name
```

## Workflow when debugging from a symptom

1. Start broad: `web --level error --since <window>` to find the failure.
2. Grab the `trace_id`/`request_id` from the metadata line.
3. Re-query by that id with `--forward` to read the full request in order,
   spanning `web` → `celery_default` if work was dispatched.
4. Use `--print-query` to inspect/borrow the LogQL; use `--json` when you need
   exact timestamps or fields the pretty output omits.

## Notes

- Retention is **30 days**; queries older than that return nothing.
- A query needs at least one matcher — with no `SERVICE`/`--level` the script
  defaults to `{service_name=~".+"}` (all app streams).
- Hitting `--limit` prints a stderr hint; narrow `--since` or raise `-n`.
- Read-only. The token is Viewer-scoped; this cannot modify anything.
