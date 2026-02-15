# Development Commands

All development tasks are driven through `make` targets. This page is the complete reference.

!!! tip "Before every commit"
    Run `make check` to catch formatting, linting, type errors, and translation issues before they hit CI:

    ```bash
    make check
    ```

## Primary Commands

These are the commands you will use most often during day-to-day development.

| Command | Description |
|---------|-------------|
| `make setup` | Complete one-time setup: creates venv, installs deps, starts Docker, applies migrations, seeds database, starts server |
| `make run` | Start the Django development server (also generates test JWTs) |
| `make check` | Run **all** code quality checks: format, lint, mypy, i18n-check |
| `make test` | Run the full pytest suite with coverage reporting |
| `make test-failed` | Re-run only previously failed tests |

## Code Quality and Formatting

| Command | Description |
|---------|-------------|
| `make format` | Auto-format code with ruff |
| `make lint` | Lint with ruff and auto-fix issues |
| `make mypy` | Run strict type checking with mypy |

!!! info "Ruff handles both formatting and linting"
    This project uses [ruff](https://docs.astral.sh/ruff/) as the single tool for both code formatting and linting, replacing the older black + flake8 + isort combination.

## Internationalization (i18n)

| Command | Description |
|---------|-------------|
| `make makemessages` | Extract translatable strings from code and templates, updating `.po` files |
| `make compilemessages` | Compile `.po` files into `.mo` binaries |
| `make i18n-check` | Verify that `.mo` files are up-to-date with their `.po` sources |

!!! note "Commit compiled translations"
    After running `make compilemessages`, **commit the generated `.mo` files**.
    The CI pipeline runs `make i18n-check` and will fail if compiled translations are out of date.

## Database Management

| Command | Description |
|---------|-------------|
| `make migrations` | Create new Django migration files for model changes |
| `make migrate` | Apply all pending migrations |
| `make bootstrap` | Initialize the database with base data (admin user, site settings) |
| `make seed` | Populate the database with sample test data |

!!! danger "Destructive database commands"
    The following commands will **destroy data**. Use them only when you need a completely fresh start.

    | Command | Description |
    |---------|-------------|
    | `make nuke-db` | Deletes the database **and** all migration files |
    | `make restart` | Restarts Docker containers and recreates the database from scratch |

    After running either of these, you will need to run `make migrate`, `make bootstrap`, and optionally `make seed` to restore a working database.

## Background Services

Revel uses Celery for asynchronous task processing. These commands start the various Celery components:

| Command | Description |
|---------|-------------|
| `make run-celery` | Start a Celery worker to process background tasks |
| `make run-celery-beat` | Start the Celery beat scheduler for periodic tasks |
| `make run-flower` | Start the Flower web UI for monitoring Celery workers and tasks |

!!! tip "Running the full stack locally"
    For a complete local environment, open separate terminal tabs and run:

    ```bash
    # Tab 1 -- Django server
    make run

    # Tab 2 -- Celery worker
    make run-celery

    # Tab 3 -- Celery beat (if you need periodic tasks)
    make run-celery-beat
    ```

## Testing

| Command | Description |
|---------|-------------|
| `make test` | Run the full test suite with coverage reporting |
| `make test-failed` | Re-run only tests that failed in the previous run |
| `make test-functional` | Run functional tests only |
| `make test-pipeline` | Run tests with a **100% coverage requirement** (used in CI) |

## Authentication

| Command | Description |
|---------|-------------|
| `make jwt EMAIL=user@example.com` | Generate JWT access and refresh tokens for a given user |

!!! info "Using JWT tokens"
    The generated access token can be used in the Swagger UI or with `curl`:

    ```bash
    curl -H "Authorization: Bearer <access_token>" http://localhost:8000/api/...
    ```

## Documentation

| Command | Description |
|---------|-------------|
| `make serve-docs` | Serve the MkDocs documentation site locally with live reload |
| `make build-docs` | Build the static documentation site for deployment |

## Common Workflows

### Starting fresh after a `main` rebase

```bash
make migrate          # Apply any new migrations from main
make bootstrap        # Re-seed base data if needed
make run              # Start the server
```

### Full reset when things go wrong

```bash
make restart          # Nuke Docker + DB, start fresh
make migrate          # Recreate schema
make bootstrap        # Base data
make seed             # Sample data
make run              # Start the server
```

### Pre-commit checklist

```bash
make check            # Format + lint + mypy + i18n-check
make test             # Run the test suite
```
