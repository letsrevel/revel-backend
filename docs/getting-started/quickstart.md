# Quickstart

Get a fully working Revel development environment up and running in under five minutes.

## Prerequisites

Before you begin, make sure the following tools are installed on your machine:

| Tool | Version | Purpose |
|------|---------|---------|
| **Make** | Any | Task runner for development commands |
| **Docker** | 20+ | Runs PostgreSQL, Redis, ClamAV, Mailpit |
| **Python** | 3.13+ | Runtime for the Django application |
| **UV** | Latest | Dependency management (replaces pip) |

!!! note "Installing UV"
    If you don't have UV installed, follow the [official instructions](https://docs.astral.sh/uv/getting-started/installation/).
    On macOS you can simply run:

    ```bash
    brew install uv
    ```

## Clone and Setup

```bash
# 1. Clone the repository
git clone git@github.com:letsrevel/revel-backend.git
cd revel-backend
```

!!! warning "Geo data file required before setup"
    The database migration `0002_load_cities` requires a cities CSV file that is **not** included in the repository due to licensing.
    **`make setup` will fail** if this file is missing.

    Download `worldcities.csv` from [SimpleMaps](https://simplemaps.com/data/world-cities) and place it at:

    ```
    src/geo/data/worldcities.csv
    ```

    For a lighter alternative during development, rename the included `worldcities.mini.csv` (a small subset):

    ```bash
    cp src/geo/data/worldcities.mini.csv src/geo/data/worldcities.csv
    ```

    The IP-to-location database (`IP2LOCATION-LITE-DB5.BIN`) is **not** required for setup. It is downloaded automatically by a periodic Celery task if the `IP2LOCATION_TOKEN` environment variable is set.

```bash
# 2. Run the one-time setup
make setup
```

That's it. The `setup` target handles everything else automatically.

!!! tip "What `make setup` does behind the scenes"
    The setup command runs several steps in sequence:

    1. **Installs all dependencies** (production + development) via `uv sync` (also creates `.venv/` if needed)
    2. **Copies `.env.example` to `.env`** for local configuration
    3. **Restarts Docker services**: PostgreSQL (with PostGIS), Redis, ClamAV, and Mailpit
    4. **Bootstraps the database**: runs migrations, creates the admin user, and loads sample event data
    5. **Starts the Django development server** with observability disabled

## Start the Development Server

After the initial setup, you can start the server on subsequent sessions with:

```bash
make run
```

The server starts at [http://localhost:8000](http://localhost:8000).

## What's Available

Once the server is running, you have access to several services:

| Service | URL | Description |
|---------|-----|-------------|
| **API** | [localhost:8000/api/](http://localhost:8000/api/) | REST API endpoints |
| **Swagger UI** | [localhost:8000/api/docs](http://localhost:8000/api/docs) | Interactive API documentation |
| **Django Admin** | [localhost:8000/admin/](http://localhost:8000/admin/) | Admin interface |
| **Mailpit** | [localhost:8025](http://localhost:8025) | Email testing inbox |

!!! info "Default admin credentials"
    A default admin account is created during bootstrap:

    - **Email:** `admin@letsrevel.io`
    - **Password:** `password`

    You can generate JWT tokens for any user with:

    ```bash
    make jwt EMAIL=admin@letsrevel.io
    ```

!!! warning "Observability is disabled by default"
    `make setup` starts the server with `ENABLE_OBSERVABILITY=False` because the observability stack (Loki, Tempo, Prometheus) is not running under `compose.yaml`. However, `.env.example` sets `ENABLE_OBSERVABILITY=True`. If you see connection errors in logs after running `make run` on subsequent sessions, either:

    - Set `ENABLE_OBSERVABILITY=False` in your `.env`, or
    - Start the observability stack: `docker compose -f docker-compose-observability.yml up -d`

## Next Steps

- [Project Structure](project-structure.md): Understand how the codebase is organized
- [Development Commands](development-commands.md): Full reference for all `make` targets
