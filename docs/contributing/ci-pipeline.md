# CI Pipeline

The Revel backend uses **GitHub Actions** for continuous integration. Every pull request
(and manual dispatch) triggers the pipeline to verify code quality, type safety,
translations, and test coverage.

---

## Pipeline Stages

### 1. Code Quality (`make check`)

The `make check` command runs eight checks in sequence:

| Check | Tool | What It Does |
|---|---|---|
| **Format** | ruff | Auto-formats code to match project style (`ruff format .`) |
| **Lint** | ruff | Catches code quality issues and anti-patterns |
| **Type check** | mypy (strict) | Validates type annotations across the entire codebase |
| **Migration check** | Django | Verifies no migrations are missing (`makemigrations --check`) |
| **i18n check** | custom | Ensures every `_()` string is extracted and every `.po` entry is translated (no empty `msgstr` / fuzzy) — no `.mo` involved |
| **i18n literals** | custom | Ensures user-facing error messages are wrapped in `str(_(...))` (`scripts/check_i18n_literals.py`) |
| **File length** | custom | Ensures no source file exceeds 1,000 lines |
| **Task names** | custom | Ensures every `@shared_task` pins an explicit `name=` (`scripts/check_task_names.py`) |

!!! tip "Run Locally First"

    Always run `make check` before pushing. It catches the same issues CI will flag,
    saving you a round-trip.

!!! note "Local vs CI Behavior"

    Locally, `make check` **auto-fixes** formatting and lint issues (`ruff format .` and `ruff check . --fix`).
    In CI, formatting is checked with `ruff format --check .` (read-only) and linting with `ruff check .` (no `--fix`). Both will fail if any changes are needed.
    This means running `make check` locally may modify files. Review and commit any changes before pushing.

### 2. Tests

CI splits the unit suite into **4 shards** (`pytest-split`, balanced by the committed
`.test_durations` file, refreshed weekly). Each shard runs
`-m "not integration and not einvoice_conformance"` and uploads its coverage data. A separate
**Combined coverage gate** job then runs `coverage combine` and
`coverage report --fail-under=90` (branch coverage), so any drop below 90% fails the pipeline.
No single shard enforces the threshold on its own.

A separate **EN 16931 conformance (Factur-X)** job runs `pytest -m einvoice_conformance`,
validating generated Factur-X documents against the CEN rules with the pinned `euinvoice`
artifacts.

### 3. Lockfile Consistency

CI verifies that `uv.lock` is consistent with `pyproject.toml`. If you've changed
dependencies, make sure to run `uv sync` and commit the updated lockfile.

### Other workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `deps.yaml` / `deps-noop.yaml` | PRs and pushes to `main` touching `pyproject.toml`, `uv.lock`, `Makefile` (the no-op twin reports success when they are untouched) | `make licensecheck` and `make audit` |
| `nightly-audit.yml` | Daily 03:17 UTC | `pip-audit` and `licensecheck` on `main`; opens a tracker issue on failure |
| `bandit.yaml` | PRs and pushes touching `src/**/*.py` or dependencies | Bandit security scan |
| `refresh-test-durations.yaml` | Weekly (Monday) | Refreshes `.test_durations` for shard balancing |
| `publish.yaml` | GitHub release published | Builds and pushes the multi-arch image (amd64 + best-effort arm64) to GHCR |
| `release.yaml` | Merge of a `release/vX.Y.Z` PR | Tags and creates the GitHub release |
| `docs.yaml` | PRs and pushes touching `docs/**` or `mkdocs.yml` | Builds (and on `main`, deploys) the docs |

---

## CI Environment

### Container Image

The lint, test and conformance jobs run inside a pre-built image
(`ghcr.io/letsrevel/revel-backend-ci`, defined by `ci.Dockerfile`) that carries every
system package (GDAL, gettext, WeasyPrint libs), so CI never runs `apt-get`. The image
is tagged by the hash of `ci.Dockerfile` and rebuilt by `.github/workflows/ci-image.yaml`
only when that file changes. Bump its "Security refresh" date to pick up fresh packages.

### Services

Jobs start these as GitHub Actions `services:` (reached by service name, e.g. `DB_HOST=postgres`):

| Service | Purpose |
|---|---|
| **PostgreSQL** (with PostGIS) | Database |
| **Redis** | Cache and Celery broker |

ClamAV is not started: the autouse `mock_clamav` fixture in `src/conftest.py` mocks it.
`docker-compose-ci.yml` is no longer used by GitHub Actions.

!!! note "No Observability Stack in CI"

    The observability services (Loki, Tempo, Prometheus, Grafana, Pyroscope) are
    **not** started in CI. The environment variable `FEATURE_OBSERVABILITY=False`
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

- [ ] `make check`: format + lint + mypy + migration-check + i18n-check + i18n-literals + file-length + task-names
- [ ] `make test`: all tests pass with 90%+ coverage
- [ ] No file exceeds 1,000 lines
