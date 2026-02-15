# CI Pipeline

The Revel backend uses **GitHub Actions** for continuous integration. Every pull request
(and manual dispatch) triggers the pipeline to verify code quality, type safety,
translations, and test coverage.

---

## Pipeline Stages

### 1. Code Quality (`make check`)

The `make check` command runs six checks in sequence:

| Check | Tool | What It Does |
|---|---|---|
| **Format** | ruff | Auto-formats code to match project style (`ruff format .`) |
| **Lint** | ruff | Catches code quality issues and anti-patterns |
| **Type check** | mypy (strict) | Validates type annotations across the entire codebase |
| **Migration check** | Django | Verifies no migrations are missing (`makemigrations --check`) |
| **i18n check** | custom | Ensures compiled `.mo` files are up-to-date with `.po` sources |
| **File length** | custom | Ensures no source file exceeds 1,000 lines |

!!! tip "Run Locally First"

    Always run `make check` before pushing. It catches the same issues CI will flag,
    saving you a round-trip.

!!! note "Local vs CI Behavior"

    Locally, `make check` **auto-fixes** formatting and lint issues (`ruff format .` and `ruff check . --fix`).
    In CI, formatting is checked with `ruff format --check .` (read-only) and linting with `ruff check .` (no `--fix`). Both will fail if any changes are needed.
    This means running `make check` locally may modify files. Review and commit any changes before pushing.

### 2. Tests

CI runs the full pytest suite in parallel with a **90% branch coverage** requirement:

```
pytest -n auto --cov=src --cov-branch --cov-fail-under=90
```

Any drop below 90% coverage fails the pipeline.

### 3. Lockfile Consistency

CI verifies that `uv.lock` is consistent with `pyproject.toml`. If you've changed
dependencies, make sure to run `uv sync` and commit the updated lockfile.

---

## CI Environment

### Docker Services

The CI environment uses `docker-compose-ci.yml` with only the services needed for
testing:

| Service | Purpose |
|---|---|
| **PostgreSQL** (with PostGIS) | Database |
| **Redis** | Cache and Celery broker |
| **ClamAV** | File malware scanning |

!!! note "No Observability Stack in CI"

    The observability services (Loki, Tempo, Prometheus, Grafana, Pyroscope) are
    **not** started in CI. The environment variable `ENABLE_OBSERVABILITY=False`
    disables all observability instrumentation during test runs.

---

## Commit Conventions

!!! info "Recommended"

    The project follows [Conventional Commits](https://www.conventionalcommits.org/)
    for commit messages, though this is not enforced by a pre-commit hook.

Common prefixes:

| Prefix | Usage |
|---|---|
| `feat:` | New feature |
| `fix:` | Bug fix |
| `refactor:` | Code restructuring without behavior change |
| `test:` | Adding or updating tests |
| `docs:` | Documentation changes |
| `chore:` | Maintenance tasks (deps, config, CI) |
| `dev:` | Developer tooling and experience improvements |

Example:

```
feat(events): add waitlist support for sold-out tiers

Allows users to join a waitlist when all tickets for a tier are sold out.
Waitlisted users are notified automatically when spots open up.
```

---

## Quick Checklist

Before opening a pull request, verify locally:

- [ ] `make check`: format + lint + mypy + migration-check + i18n-check + file-length
- [ ] `make test` (or `make test-parallel`): all tests pass with 90%+ coverage
- [ ] No file exceeds 1,000 lines
