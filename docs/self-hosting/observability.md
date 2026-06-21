# Observability

Revel ships a full LGTM-style observability stack, but it is off by default on Slim instances
because it costs several gigabytes of RAM. Turn it on when you want dashboards, log search, and
distributed traces.

## Enabling the stack

Add the `observability` profile to your `COMPOSE_PROFILES` in `.env`:

```bash
COMPOSE_PROFILES=observability
```

(Combine it with other profiles as needed, e.g. `observability,antivirus`.) Then re-run
`docker compose up -d` to start the additional services.

The application-side toggle is the `FEATURE_OBSERVABILITY` environment variable, which enables the
metrics/traces/logs exporters and the async log queue inside the Django and Celery processes:

```bash
FEATURE_OBSERVABILITY=True
```

!!! note "Deprecated alias"
    The legacy `ENABLE_OBSERVABILITY` variable is still honored as a deprecated alias, but new
    configurations should use `FEATURE_OBSERVABILITY`. Keep the flag and the `observability` profile
    in sync — running the exporters with no stack to receive them just produces connection errors.

## What each tool does

- **Prometheus** — scrapes and stores time-series metrics from the services.
- **Loki** — aggregates and indexes logs so you can search across all containers.
- **Tempo** — stores distributed traces for following a request across services.
- **Pyroscope** — continuous profiling, for finding CPU/memory hot spots.
- **Alloy** — the collection agent that ships metrics, logs, and traces into the above.
- **Grafana** — the single UI that ties Prometheus, Loki, Tempo, and Pyroscope together with
  dashboards and explore views.

## Grafana login

Grafana's admin credentials are set via environment variables in `.env`:

- `GF_SECURITY_ADMIN_USER` — the admin username.
- `GF_SECURITY_ADMIN_PASSWORD` — the admin password.

Set these before first start; once Grafana initializes its database, changing the password is done
in the Grafana UI rather than via the env var. Grafana is reachable at `https://grafana.<your-domain>`
once DNS and certificates are in place.

## Resource cost

!!! note "Why it's off by default"
    The observability stack adds **several GB of RAM** on top of the core services — enough that
    it does not fit comfortably on a 4 GB Slim box. That is the entire reason it lives behind a
    profile rather than running always. Enable it on the Full tier, or on a Slim box only after
    you have given it more memory.
