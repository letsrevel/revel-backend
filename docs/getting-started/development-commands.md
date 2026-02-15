# Development Commands

All development tasks are driven through `make` targets. This page is the complete reference.

!!! tip "Before every commit"
    Run `make check` to catch formatting, linting, type errors, migration issues, and file length violations before they hit CI:

    ```bash
    make check
    ```

## Primary Commands

These are the commands you will use most often during day-to-day development.

| Command | Description |
|---------|-------------|
| `make setup` | Complete one-time setup: installs deps, copies `.env.example`, starts Docker, bootstraps database, starts server |
| `make run` | Start the Django development server (also generates test JWTs) |
| `make check` | Run **all** code quality checks (see below) |
| `make test` | Run the full pytest suite with coverage reporting |
| `make test-parallel` | Run the full test suite in parallel (`pytest -n auto`) |
| `make test-failed` | Re-run only previously failed tests |

## Code Quality and Formatting

`make check` runs all of the following in sequence:

| Step | Command | What It Does |
|------|---------|-------------|
| 1 | `make format` | Auto-format code with ruff |
| 2 | `make lint` | Lint with ruff and auto-fix issues |
| 3 | `make mypy` | Run strict type checking with mypy |
| 4 | `make migration-check` | Verify no migrations are missing (`makemigrations --check`) |
| 5 | `make i18n-check` | Verify compiled `.mo` files are up-to-date |
| 6 | `make file-length` | Verify no source file exceeds 1,000 lines |

!!! info "Why a custom file length check?"
    Ruff doesn't currently enforce a maximum file length. Rather than pulling in another linter just for this one rule, we use a lightweight shell script (`scripts/check-file-length.sh`) that fails if any Python file exceeds 1,000 lines.

!!! info "Ruff handles both formatting and linting"
    This project uses [ruff](https://docs.astral.sh/ruff/) as the single tool for both code formatting and linting, replacing the older black + flake8 + isort combination.

## Internationalization (i18n)

| Command | Description |
|---------|-------------|
| `make makemessages` | Extract translatable strings from code and templates, updating `.po` files |
| `make compilemessages` | Compile `.po` files into `.mo` binaries |
| `make i18n-check` | Verify that `.mo` files are up-to-date with their `.po` sources |

!!! warning "Commit compiled translations"
    After running `make compilemessages`, **commit the generated `.mo` files**.
    The CI pipeline runs `make i18n-check` and will fail if compiled translations are out of date.

## Database Management

| Command | Description |
|---------|-------------|
| `make migrations` | Create new Django migration files for model changes |
| `make migrate` | Apply all pending migrations |
| `make bootstrap` | Full database initialization: runs `migrate`, creates admin user, loads sample events and test data, generates test JWTs |
| `make seed` | Populate the database with large-scale faker-generated data (useful for load testing) |
| `make reset-events` | Reset event data to bootstrap defaults (requires `DEMO_MODE=True`) |

!!! danger "Destructive database commands"
    The following commands will **destroy data**. Use them only when you need a completely fresh start.

    | Command | Description |
    |---------|-------------|
    | `make nuke-db` | Deletes the database and migration files (preserving special data migrations), regenerates migrations, restarts Docker, and runs migrate |
    | `make restart` | **Most destructive**: Deletes the database, removes **all** migration files, regenerates them, restarts Docker, and runs bootstrap |
    | `make flush` | Flushes all data from the database (keeps schema) |

    After running `make nuke-db` or `make restart`, run `make bootstrap` to restore a working database.

## Background Services

Revel uses Celery for asynchronous task processing. In development, Celery runs in **eager mode** by default (`CELERY_TASK_ALWAYS_EAGER=True` when `DEBUG=True`), meaning tasks execute synchronously in the same process — no separate workers needed.

To test with real async processing, set `CELERY_TASK_ALWAYS_EAGER=False` in your `.env` and start the workers:

| Command | Description |
|---------|-------------|
| `make run-celery` | Start a Celery worker to process background tasks |
| `make run-celery-beat` | Start the Celery beat scheduler for periodic tasks |
| `make run-flower` | Start the Flower web UI for monitoring Celery workers and tasks |
| `make run-telegram` | Start the Telegram bot (long-polling mode) |
| `make run-stripe` | Start the Stripe webhook listener (forwards to localhost) |

!!! tip "Running the full stack locally"
    For a complete local environment with real async processing, open separate terminal tabs:

    ```bash
    # Tab 1 -- Django server
    make run

    # Tab 2 -- Celery worker
    make run-celery

    # Tab 3 -- Celery beat (if you need periodic tasks)
    make run-celery-beat

    # Tab 4 -- Telegram bot (if working on Telegram features)
    make run-telegram
    ```

## Testing

| Command | Description |
|---------|-------------|
| `make test` | Run the full test suite with coverage reporting |
| `make test-parallel` | Run tests in parallel with `pytest -n auto` |
| `make test-failed` | Re-run only tests that failed in the previous run |

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
| `make serve-docs` | Serve the MkDocs documentation site locally at [localhost:8800](http://localhost:8800) |
| `make build-docs` | Build the static documentation site for deployment |

## Utility Commands

| Command | Description |
|---------|-------------|
| `make shell` | Open the Django interactive shell |
| `make reset-db` | Reset the database using the `reset_db` management command |
| `make db-diagram` | Generate a database entity-relationship diagram (requires `django-extensions`) |
| `make bootstrap-tests` | Run only the `bootstrap_tests` management command (test events for eligibility gates) |
| `make check-version` | Check the currently deployed versions on main and dev servers |
| `make count-lines` | Count non-blank, non-comment Python lines in `src/` |
| `make tree` | Print the project directory tree (excludes `__pycache__`, images, htmlcov) |
| `make dump-openapi` | Dump the OpenAPI schema to a file |
| `make dump-issues` | Export all open GitHub issues to `issues.md` |
| `make release` | Create a GitHub release for the current version |

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
make bootstrap        # Recreate schema + base data (includes migrate)
make run              # Start the server
```

!!! warning "Ephemeral database"
    In development, PostgreSQL uses **tmpfs** (in-memory storage). All database data is lost when the Docker container stops. After any `docker compose down`, you'll need to run `make bootstrap` to restore the database.

### Pre-commit checklist

```bash
make check            # Format + lint + mypy + migration-check + i18n-check + file-length
make test             # (or make test-parallel)
```
