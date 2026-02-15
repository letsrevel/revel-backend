# Quickstart

Get a fully working Revel development environment up and running in under five minutes.

## Prerequisites

Before you begin, make sure the following tools are installed on your machine:

| Tool | Version | Purpose |
|------|---------|---------|
| **Make** | Any | Task runner for development commands |
| **Docker** | 20+ | Runs PostgreSQL, Redis, MinIO, Mailpit |
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

# 2. Run the one-time setup
make setup
```

That's it. The `setup` target handles everything else automatically.

!!! tip "What `make setup` does behind the scenes"
    The setup command runs several steps in sequence:

    1. **Creates a virtual environment** in `.venv/` using UV
    2. **Installs all dependencies** (production + development) from the lockfile
    3. **Starts Docker services** -- PostgreSQL (with PostGIS), Redis, MinIO, and Mailpit
    4. **Applies database migrations** to create the schema
    5. **Bootstraps base data** (default admin user, site settings, etc.)
    6. **Seeds the database** with sample data for local development
    7. **Starts the Django development server**

!!! warning "Geo data files"
    Some geolocation features require data files that are **not** included in the repository due to licensing:

    - `src/geo/data/IP2LOCATION-LITE-DB5.BIN` -- IP-to-location database
    - `src/geo/data/worldcities.csv` -- World cities dataset

    The application will start without these files, but geo-related endpoints will not work correctly.
    Download them from their respective sources and place them in `src/geo/data/` before using geo features.

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

## Next Steps

- [Project Structure](project-structure.md) -- Understand how the codebase is organized
- [Development Commands](development-commands.md) -- Full reference for all `make` targets
