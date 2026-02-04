---
name: updating-deps
description: Update Python dependencies using UV. Scan for outdated packages, identify unused dependencies, and safely update pyproject.toml while respecting version constraints (e.g., Django LTS).
allowed-tools: Bash(uv:*), Bash(pip:*), Bash(make:*), Read, Grep, Edit
---

# Dependency Update Workflow

A structured approach for updating Python dependencies in this UV-managed project.

## Prerequisites

- This project uses **UV** for dependency management (never pip directly)
- Dependencies are in `pyproject.toml` under `[project] dependencies` and `[dependency-groups] dev`
- Django is pinned to 5.2.x LTS (`>=5.2.x,<6.0`)

## Step 1: Check Outdated Dependencies

```bash
uv pip list --outdated
```

This shows all outdated packages. Focus on **top-level dependencies** listed in `pyproject.toml`.

## Step 2: Identify Unused Dependencies

For each suspicious dependency, search for actual usage:

```bash
# Check for imports
grep -r "from <package>|import <package>" src/

# Check if it's a transitive dependency
uv pip show <package> | grep -i "required-by"
```

### Known Transitive Dependencies (safe to remove from explicit deps)

These are pulled in automatically by other packages:
- `multidict` - transitive from aiohttp/aiogram
- `pygments` - transitive from mkdocs-material, pytest

### Type Stubs Belong in Dev

Move `types-*` packages to `[dependency-groups] dev`, not production dependencies.

## Step 3: Categorize Updates

### Safe Updates (patch/minor, no breaking changes)
- Patch versions: `1.2.3` → `1.2.4`
- Minor versions with good release notes: `1.2.x` → `1.3.x`

### Needs Review (major versions)
- Major bumps: `1.x` → `2.x`
- Check release notes/changelog before updating

### Version-Pinned Dependencies
- **Django**: Keep at latest 5.2.x LTS (`>=5.2.x,<6.0`)
- Check classifier in pyproject.toml: `"Framework :: Django :: 5.2"`

## Step 4: Apply Updates

Edit `pyproject.toml` directly, then sync:

```bash
uv sync --dev
```

## Step 5: Verify

Run all checks to ensure nothing broke:

```bash
make check
```

This runs: format, lint, mypy, i18n-check

## Common Patterns

### Remove Unused Dependency
1. Search for usage: `grep -r "import <pkg>" src/`
2. Check reverse deps: `uv pip show <pkg> | grep Required-by`
3. Remove from pyproject.toml
4. Run `uv sync --dev`

### Move to Dev Dependencies
1. Remove from `[project] dependencies`
2. Add to `[dependency-groups] dev`
3. Run `uv sync --dev`

### Pin to Major Version
```toml
# Allow patches within LTS
"django>=5.2.11,<6.0"
```

## Dependencies Removed (2026-02-04)

For reference, these were removed as unused/transitive:
- `django-extension` - typo (django-extensions already present)
- `django-money` - unused
- `freezegun` in prod - duplicate (already in dev)
- `multidict` - transitive from aiohttp
- `ninja-schema` - unused
- `pyopenssl` - unused
- `pygments` - transitive from dev deps
- `types-tqdm` - moved to dev
- `piexif` - moved to dev (only used in tests)
