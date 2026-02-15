# Dependency Management

Revel uses [UV](https://docs.astral.sh/uv/) for all dependency management.
See [ADR-0003](../adr/0003-uv-over-pip.md) for the full rationale behind this choice.

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
| Optional group | `uv add --optional <group> <package>` |

```bash
# Add a production dependency
uv add httpx

# Add a development-only dependency
uv add --dev pytest-randomly

# Add to an optional dependency group
uv add --optional observability opentelemetry-api
```

### Removing Dependencies

```bash
uv remove <package>
```

### Syncing the Environment

| Command | Description |
|---|---|
| `uv sync` | Install all dependencies from the lockfile |
| `uv sync --dev` | Include development dependencies |

!!! tip

    After pulling changes that modify `pyproject.toml` or `uv.lock`, run
    `uv sync --dev` to ensure your local environment matches.

---

## How It Works

UV manages dependencies through two files:

- **`pyproject.toml`** -- declares dependencies and their version constraints
- **`uv.lock`** -- the resolved lockfile with exact versions (committed to git)

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
    before working on the project. The `make setup` command handles this for new
    contributors.
