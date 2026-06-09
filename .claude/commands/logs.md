---
description: Read production logs from Loki (via Grafana) — by service, level, request/trace id, time window
argument-hint: "[service] [--level error] [--grep TEXT] [--request-id ID] [--since 2h] ... (or a plain-English ask)"
allowed-tools: Bash(python:*), Bash(.venv/bin/python:*), Read, Grep
---

Invoke the `loki-logs` skill via the Skill tool, then fetch production logs for the user.

User request (may be raw `loki_logs.py` flags, or plain English): $ARGUMENTS

Guidance:
- Always use the venv interpreter: `.venv/bin/python scripts/loki_logs.py ...` (the script imports `python-decouple`).
- If `$ARGUMENTS` already looks like script flags, run `.venv/bin/python scripts/loki_logs.py $ARGUMENTS` (defaulting `--since` to `1h` only if the user gave no window).
- If it's a plain-English ask ("why did checkout 500 an hour ago", "show celery errors today"), translate it into the right `loki_logs.py` invocation per the skill, run it, and summarise what the logs show — surfacing `trace_id`/`request_id` and offering to drill in.
- If `$ARGUMENTS` is empty, default to recent errors across the API: `.venv/bin/python scripts/loki_logs.py web --level error --since 1h`.
- If the script reports `GRAFANA_TOKEN is not set`, stop and tell the user how to set it up (Grafana → Service accounts → Viewer token → add `GRAFANA_TOKEN=<token>` to `.env`). Do not fabricate logs.
