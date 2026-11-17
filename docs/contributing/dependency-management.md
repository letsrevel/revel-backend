# Dependency Management

Revel uses [UV](https://docs.astral.sh/uv/) for all dependency management.
**Never use pip directly.**

!!! danger "Never Use pip"

    **Do not** run `pip install`, `pip freeze`, or any pip command directly.
    All dependency operations go through UV.

---

## Commands

### Adding Dependencies

| Dependency Type | Command |
|---|---|
| Production | `uv add <package>` |
| Development | `uv add --dev <package>` |
| Development (explicit group) | `uv add --group dev <package>` |
```bash
# Add a production dependency
uv add httpx

# Add a development-only dependency
uv add --dev pytest-randomly
```

### Removing Dependencies

```bash
uv remove <package>
```

### Syncing the Environment

| Command | Description |
|---|---|
| `uv sync` | Install all dependencies from the lockfile (includes dev dependencies by default) |
| `uv sync --group dev` | Explicitly include the dev dependency group |

!!! tip

    After pulling changes that modify `pyproject.toml` or `uv.lock`, run
    `uv sync` to ensure your local environment matches.

---

## Version Bumps

Never hand-edit `version` in `pyproject.toml`. Use `uv version --bump <major|minor|patch>` so
`uv.lock` stays in sync. Releases go through the `/release` PR flow (see
[Development Commands](../getting-started/development-commands.md#releases)).

## Django Version Pin

Django is pinned `>=5.2.19,<6.0`: Revel stays on the 5.2 LTS line. Do not widen the upper bound
without a new decision; see [ADR-0005](../adr/0005-django-lts-policy.md).

## Compliance Checks

| Command | Description |
|---|---|
| `make deps-check` | Runs `licensecheck` and `audit` |
| `make licensecheck` | Fails on copyleft / source-available licenses entering the tree |
| `make audit` | `pip-audit` over the locked graph; fails on known CVEs |

These hit the network, so they are not part of `make check`. CI runs them on dependency changes
and nightly. Triage steps and the grandfathering policy are in the
[dependency audit runbook](../runbooks/dependency-audit.md).

---

## How It Works

UV manages dependencies through two files:

- **`pyproject.toml`**: declares dependencies and their version constraints
- **`uv.lock`**: the resolved lockfile with exact versions (committed to git)

This replaces the traditional `requirements.txt` / `requirements-dev.txt` workflow.

---

## Why UV?

| Benefit | Details |
|---|---|
| **Speed** | 10-100x faster than pip for installs and resolution |
| **Deterministic resolution** | Lockfile guarantees identical environments everywhere |
| **Automatic venv management** | Creates and manages `.venv/` automatically |
| **Better conflict detection** | Catches dependency conflicts early with clear error messages |

!!! info

    Contributors need to [install UV](https://docs.astral.sh/uv/getting-started/installation/)
    before working on the project. Once UV is installed, run `make setup` to
    complete the environment configuration.
