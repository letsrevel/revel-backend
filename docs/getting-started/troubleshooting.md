# Troubleshooting

Common gotchas and their solutions. Most of these surface during initial setup or after a Docker restart.

---

## Geo Data Files

The geo app requires two data files in `src/geo/data/`:

| File | Purpose | Required By |
|------|---------|-------------|
| `worldcities.csv` | City database (names, coords, population) | Migration `0002_load_cities` |
| `IP2LOCATION-LITE-DB5.BIN` | IP-to-location mapping | Runtime geolocation lookups |

### Cities CSV missing

Migration `0002_load_cities` will fail with a `FileNotFoundError` if the CSV is not present:

```
FileNotFoundError: City import failed: file does not exist at /app/src/geo/data/worldcities.csv
```

!!! tip "Quick fix"
    Download the file from [SimpleMaps](https://simplemaps.com/data/world-cities) and place it at `src/geo/data/worldcities.csv`. For development, `worldcities.mini.csv` (a smaller subset) also works — rename it to `worldcities.csv`.

### IP2Location database

A Celery periodic task downloads a fresh IP2Location database every Monday at 04:00 UTC. It requires the `IP2LOCATION_TOKEN` environment variable.

```bash
# .env
IP2LOCATION_TOKEN=your-token-here
```

!!! warning "Docker volume permissions"
    The download task writes to `src/geo/data/` inside the container. If you mount this directory as a volume, ensure the container user (`appuser`) has **read and write** access. Without write access the download silently fails and stale data is served.

    ```yaml
    # docker-compose example
    volumes:
      - ./src/geo/data:/app/src/geo/data  # must be writable by appuser
    ```

---

## Apple Wallet Certificates

Apple Wallet pass generation requires three PEM certificate files and several environment variables.

### Required environment variables

```bash
# .env
APPLE_TEAM_IDENTIFIER=YOUR_TEAM_ID
APPLE_WALLET_PASS_TYPE_ID=pass.io.letsrevel.events
APPLE_WALLET_TEAM_ID=YOUR_TEAM_ID
APPLE_WALLET_CERT_PATH=/app/src/certs/pass_certificate.pem
APPLE_WALLET_KEY_PATH=/app/src/certs/pass_key.pem
APPLE_WALLET_KEY_PASSWORD=           # can be empty string
APPLE_WALLET_WWDR_CERT_PATH=/app/src/certs/wwdr.pem
```

### Required certificate files

Place these in `src/certs/`:

| File | Description |
|------|-------------|
| `pass_certificate.pem` | Your pass type certificate from Apple Developer |
| `pass_key.pem` | Private key for the pass certificate |
| `wwdr.pem` | Apple Worldwide Developer Relations certificate |

!!! warning "File permissions in Docker"
    Certificate files must be readable by `appuser` inside the container. Set permissions before building:

    ```bash
    chmod 644 src/certs/pass_certificate.pem src/certs/wwdr.pem
    chmod 600 src/certs/pass_key.pem  # private key — restrict access
    ```

!!! info "Feature is optional"
    If the environment variables are not set, the wallet feature is simply disabled. The app starts normally — you'll only see errors if you try to generate a pass without the configuration.

---

## Docker Permissions

The production Docker image runs as a non-root user (`appuser`). This is the most common source of permission errors.

### The `appuser` setup

```dockerfile
# From Dockerfile
RUN useradd --create-home --system --shell /bin/bash appuser
# ...
USER appuser
```

All files copied into the image use `--chown=appuser:appuser`, but **mounted volumes** inherit permissions from the host.

### Fixing volume permission issues

If you see `PermissionError` or `EACCES` in logs:

=== "Linux"

    ```bash
    # Find appuser's UID inside the container
    docker compose exec web id appuser
    # uid=999(appuser) gid=999(appuser)

    # Fix host directory ownership
    sudo chown -R 999:999 ./src/geo/data ./src/certs
    ```

=== "macOS"

    Docker Desktop for Mac uses a VM with gRPC-FUSE file sharing, so host UIDs don't map 1:1. Permissions usually just work. If they don't:

    ```bash
    chmod -R a+rw ./src/geo/data ./src/certs
    ```

### Volumes that need write access

| Path | Why |
|------|-----|
| `src/geo/data/` | Celery downloads IP2Location database updates |
| Media directory | User-uploaded files (profile photos, event images) |

### Volumes that are read-only

| Path | Why |
|------|-----|
| `src/certs/` | Apple Wallet certificates — never modified at runtime |

!!! note "tmpfs and data loss"
    The development Docker setup uses **tmpfs** for PostgreSQL (configured in `docker-compose-base.yml`), meaning the database lives in memory. Data is lost when the container stops. This is intentional for fast test cycles — use `make bootstrap` to repopulate after a restart.

---

## External Services

Most external services have safe defaults for local development. Here's what actually needs configuration:

### Required for any development

| Service | Env Var | Default | Notes |
|---------|---------|---------|-------|
| PostgreSQL | `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_PORT` | `revel`, `db-password`, `revel`, `5432` | Provided by Docker Compose (values from `.env.example`) |
| Redis | `REDIS_HOST` | `localhost` | Provided by Docker Compose |

### Required for specific features

| Service | Env Var | Default | Feature |
|---------|---------|---------|---------|
| Stripe | `STRIPE_SECRET_KEY` | Test key (`sk_test_...`) | Paid ticket checkout |
| Telegram | `TELEGRAM_BOT_TOKEN` | `0000000000:AABBCCDD` | Telegram bot |
| OpenAI | `OPENAI_API_KEY` | `fake-key` | Questionnaire AI evaluation |
| Google SSO | `GOOGLE_SSO_CLIENT_ID` | `fake-id` | Google login |
| IP2Location | `IP2LOCATION_TOKEN` | `None` | IP geolocation |
| HuggingFace | `HUGGING_FACE_HUB_TOKEN` | **None** | Sentinel model download |

!!! info "Telegram token"
    The default `TELEGRAM_BOT_TOKEN` is a non-functional placeholder. The app starts fine with it, but Telegram features won't work. To develop with a real bot, create one via [@BotFather](https://t.me/BotFather) and set the token in your `.env`.

!!! tip "Stripe test mode"
    The default Stripe keys are test keys (prefixed `sk_test_` / `pk_test_`). These are safe for development — no real charges are made.

---

## Feature Flags

Flags that affect development behavior:

| Flag | Default | What it does |
|------|---------|-------------|
| `DEMO_MODE` | `True` in dev | Exposes a fake login endpoint (no password required) |
| `SYSTEM_TESTING` | `False` | Exposes verification tokens in response headers |
| `DISABLE_THROTTLING` | `False` | Removes all rate limiting |
| `SILK_PROFILER` | `False` | Enables Django Silk profiling UI |

!!! danger "Production safety"
    `DEMO_MODE` and `SYSTEM_TESTING` must be `False` in production. `DEMO_MODE` defaults to `True` when `DEBUG=True`, so it's safe in development, but always verify production `.env` files.
