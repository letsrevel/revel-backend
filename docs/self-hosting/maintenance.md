# Maintenance

Day-to-day operation of a self-hosted instance: backups and restores, upgrades, refreshing geo
data, and the dataset attribution you are required to keep.

## Backups

The `infra` repo ships a `deploy.sh` helper that wraps the common operations.

```bash
./deploy.sh backup
```

This dumps the PostgreSQL database to a timestamped file. Run it on a schedule (cron) and copy the
dumps off the box — a backup that only lives on the server it backs up is not a backup.

### Restore

Restore a dump with `psql` against the running Postgres container:

```bash
# Copy the dump into the container (or mount it), then:
docker compose exec -T revel_postgres psql -U <DB_USER> -d <DB_NAME> < backup.sql
```

Restore into an empty database. If you are recovering onto a fresh box, bring the stack up so the
schema migrations have run, then load the dump.

!!! warning "Test your restores"
    A backup you have never restored is a hope, not a backup. Periodically restore a dump into a
    throwaway database and confirm it loads cleanly.

## Upgrades

```bash
./deploy.sh update
```

This pulls the latest application images from the registry and recreates the affected services
with them (the `web` container runs any pending database migrations on startup). Read the changelog
before updating a production instance, and take a backup first.

## Refreshing geo data

Geolocation uses two datasets: the **cities** dataset (for city/region lookups) and the
**IP2Location** BIN (for IP-to-location).

- **Cities** — the full `worldcities.csv` is loaded at first migration if present, otherwise the
  bundled 50-city mini dataset is used. To upgrade from mini to full, drop the full CSV into the
  geo data directory and re-run the city-loading step.
- **IP2Location** — the BIN is refreshed by a periodic Celery task, but only if you provide a
  download token. Set the `IP2LOCATION_TOKEN` environment variable in `.env` to your IP2Location
  LITE token; the scheduled task then downloads and swaps in updated BIN files automatically. Without
  the token, IP lookups simply return `None` and nothing refreshes.

!!! note "Attribution is required"
    The geo datasets (SimpleMaps world cities and IP2Location LITE) carry attribution requirements.
    The `NOTICE` file shipped with the deployment records the required attributions — **keep it in
    place and do not strip it**. If you redistribute or publicly display the data, honor the
    upstream licenses' attribution terms.

## Volumes to back up

The database dump covers your relational data, but several named Docker volumes hold state that a
SQL dump does not. Include these in your backup routine:

- **postgres data** — the database files (also covered by `deploy.sh backup`, but back up the
  volume too for a fast full restore).
- **media** — user-uploaded files (event images, logos, ticket assets).
- **caddy data/config** — issued TLS certificates and Caddy state (so you don't re-issue certs on
  every rebuild).
- **geo data** — the IP2Location BIN and any full cities CSV you added.
- **grafana / prometheus / loki / tempo** (Full tier only) — dashboards, metrics, logs, and traces,
  if you want observability history to survive a rebuild.
