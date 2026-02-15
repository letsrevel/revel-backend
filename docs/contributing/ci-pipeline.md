# CI Pipeline

The Revel backend uses **GitHub Actions** for continuous integration. Every push and
pull request triggers the pipeline to verify code quality, type safety, translations,
and test coverage.

---

## Pipeline Stages

### 1. Code Quality (`make check`)

The `make check` command runs four checks in sequence:

| Check | Tool | What It Does |
|---|---|---|
| **Format** | ruff | Verifies code formatting (auto-fixable) |
| **Lint** | ruff | Catches code quality issues and anti-patterns |
| **Type check** | mypy (strict) | Validates type annotations across the entire codebase |
| **i18n check** | custom | Ensures compiled `.mo` files are up-to-date with `.po` sources |

!!! tip "Run Locally First"

    Always run `make check` before pushing. It catches the same issues CI will flag,
    saving you a round-trip.

### 2. Tests (`make test-pipeline`)

Runs the full pytest suite with a **100% coverage** requirement.

```
pytest --cov --cov-report=html --cov-fail-under=100
```

Any uncovered line fails the pipeline.

### 3. File Length Enforcement

All source files must stay under **1,000 lines**. This is checked automatically and
will fail the build if violated.

---

## CI Environment

### Docker Services

The CI environment uses a minimal `docker-compose` configuration with only the
services needed for testing:

| Service | Purpose |
|---|---|
| **PostgreSQL** (with PostGIS) | Database |
| **Redis** | Cache and Celery broker |
| **ClamAV** | File malware scanning |

!!! note "No Observability Stack in CI"

    The observability services (OpenTelemetry Collector, Jaeger, Prometheus, Grafana)
    are **not** started in CI. The environment variable `ENABLE_OBSERVABILITY=False`
    disables all observability instrumentation during test runs.

### Environment Configuration

Key CI-specific settings:

```bash
ENABLE_OBSERVABILITY=False
DJANGO_SETTINGS_MODULE=revel.settings.test
```

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

- [ ] `make format` -- code is formatted
- [ ] `make lint` -- no linting errors
- [ ] `make mypy` -- no type errors
- [ ] `make i18n-check` -- translations are compiled
- [ ] `make test` -- all tests pass
- [ ] No file exceeds 1,000 lines
