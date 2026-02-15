# ADR-0003: UV Over pip for Dependency Management

## Status

Accepted

## Context

The project was using pip for dependency management, which presented several pain
points as the project grew:

- **Slow installs**: pip resolves and installs dependencies sequentially, making
  CI builds and local setup unnecessarily slow.
- **Non-deterministic resolution**: Without careful pinning and a lockfile, different
  environments could end up with different dependency versions.
- **Manual virtual environment management**: Developers had to create, activate, and
  maintain virtual environments manually.
- **Poor conflict detection**: pip often installs incompatible versions silently,
  surfacing errors only at runtime.

## Decision

Use **[UV](https://docs.astral.sh/uv/)** exclusively for all dependency management.
**Never use pip directly.**

Key commands:

| Operation | Command |
|---|---|
| Add production dep | `uv add <package>` |
| Add dev dep | `uv add --dev <package>` |
| Remove dep | `uv remove <package>` |
| Sync environment | `uv sync --dev` |

## Consequences

**Positive:**

- **10-100x faster** installs and dependency resolution compared to pip
- **Deterministic resolution** via `uv.lock` -- identical environments everywhere
- **Automatic virtual environment** management (creates and manages `.venv/`)
- **Better conflict detection** -- catches incompatibilities at resolution time with
  clear error messages

**Negative:**

- Newer tool with less ecosystem familiarity -- contributors need to install UV
- Some edge cases in the Python packaging ecosystem may not yet be handled

**Neutral:**

- `pyproject.toml` + `uv.lock` replaces `requirements.txt` / `requirements-dev.txt`
- The `make setup` command handles UV installation for new contributors
