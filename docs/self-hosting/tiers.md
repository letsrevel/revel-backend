# Tiers & Configuration

Revel is configured almost entirely through environment variables in `.env` and the set of Docker
Compose profiles you enable. This page documents the two reference tiers and every knob you can
turn.

## Slim vs Full

The two reference sizings differ in which optional profiles run and how generous the resource
limits are:

| Setting | Slim (~2 vCPU / 4 GB) | Full (8 vCPU / 32 GB) |
| --- | --- | --- |
| `COMPOSE_PROFILES` | *(empty)* | `observability,antivirus,flower,telegram,canary` |
| Postgres `shared_buffers` | `256MB` | `4GB` |
| Gunicorn workers | `2` | `6` |
| Celery concurrency | `2` | `4` |

Slim runs only the core services (caddy, frontend, web, celery, beat, postgres, pgbouncer, redis).
Full adds antivirus scanning, the LGTM observability stack, Flower, the Telegram bot, and the
canary.

## Compose profiles

Each optional capability is gated behind a Compose profile. Add the profile name to
`COMPOSE_PROFILES` (comma-separated) to run it:

- **observability** — Prometheus, Loki, Tempo, Pyroscope, Alloy, and Grafana. See
  [Observability](observability.md).
- **antivirus** — the ClamAV daemon used by `FEATURE_MALWARE_SCAN`.
- **flower** — the Celery monitoring UI.
- **telegram** — the Telegram bot process.
- **canary** — the synthetic-monitoring canary.

If you leave `COMPOSE_PROFILES` empty you get the Slim core. Remember to pair each profile with its
feature flag — for example, running the `antivirus` profile but leaving `FEATURE_MALWARE_SCAN=False`
just wastes RAM.

## Environment knobs

### Compose & resources

- `COMPOSE_PROFILES` — comma-separated list of optional profiles to enable (see above).
- `PG_SHARED_BUFFERS`, `PG_*` — PostgreSQL tuning (e.g. `shared_buffers`, work_mem). Slim uses
  conservative values; Full scales them up.
- Resource-limit vars — per-service CPU/memory limits (Gunicorn worker count, Celery concurrency,
  container memory ceilings) used to fit the stack onto small or large hosts.

### Domains

- `FRONTEND_DOMAIN` — public hostname of the web app.
- `API_DOMAIN` — public hostname of the API.
- Additional `*_DOMAIN` vars (`grafana`, `flower`, `docs`) for the Full-tier management UIs.

### Feature flags

These default to `True` (production behaviour). Set them to `False` to drop the corresponding
dependency:

- `FEATURE_MALWARE_SCAN` — when `False`, uploads skip the ClamAV scan and are marked clean
  immediately. Lets you run without the `antivirus` profile.
- `FEATURE_TELEGRAM` — when `False`, Telegram is stripped from notification delivery and the
  linking endpoints return 404. Pair with leaving the `telegram` profile off.
- `FEATURE_LLM_EVALUATION` — when `False`, automated questionnaire evaluation is disabled; manual
  evaluation still works. Lets you run without an OpenAI key.
- `FEATURE_ORGANIZATION_CREATION` — when `False`, regular users cannot create organizations; staff
  and superusers still can. See below.
- `FEATURE_OBSERVABILITY` — master toggle for the metrics/traces/logs exporters and the async log
  queue. Set `False` (and drop the `observability` profile) on Slim. Legacy alias:
  `ENABLE_OBSERVABILITY` (deprecated). See [Observability](observability.md).

### Email

- `EMAIL_DRY_RUN` — when `True`, outbound email is logged instead of sent. Useful for test
  instances; never use it for a real instance, since verification mails won't be delivered.
- SMTP settings — host, port, credentials, and from-address for real email.

## Single-org instances

For a community that only ever needs one organization, set:

```bash
FEATURE_ORGANIZATION_CREATION=False
```

With this flag off, normal users who hit "create organization" receive a `403`, while your staff
or superuser account can still create the single org from the admin or API. This is the
recommended configuration for a personal or single-community instance — it keeps the public
sign-up flow open while preventing arbitrary users from spinning up their own orgs on your box.
