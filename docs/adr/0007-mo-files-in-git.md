# ADR-0007: Compiled Translation Files (.mo) in Git

## Status

Accepted

## Context

Django uses two file types for internationalization:

- **`.po` files** -- human-readable translation source files
- **`.mo` files** -- compiled binary files that Django reads at runtime

The traditional approach is to `.gitignore` compiled `.mo` files and generate them
during the build process (Docker build, CI setup, deployment script). This requires
**gettext tools** to be installed in every environment that needs to run the
application.

Problems with the traditional approach:

- Docker builds need `gettext` installed, adding image size and build complexity
- CI environments need `gettext` to compile translations before testing
- New developers need `gettext` on their machines
- Forgetting to compile after editing `.po` files leads to stale translations

## Decision

**Commit `.mo` files to version control** alongside their `.po` source files.

The CI pipeline verifies that `.mo` files are up-to-date with their `.po` sources
using `make i18n-check`. If a developer edits a `.po` file but forgets to recompile,
CI catches it.

### Workflow

1. Edit `.po` files (manually or via `make makemessages`)
2. Run `make compilemessages` to regenerate `.mo` files
3. Commit both `.po` and `.mo` files together

## Consequences

**Positive:**

- **Reproducible builds everywhere** -- no gettext dependency in Docker, CI, or
  developer machines
- **Docker builds are simpler** -- no compilation step needed
- **CI can verify translations** -- `make i18n-check` confirms freshness
- **Consistent for all developers** -- what you see in git is what runs

**Negative:**

- Binary files in git (though small, typically ~50 KB each)
- Requires discipline to recompile after editing `.po` files

**Neutral:**

- CI enforces `.mo` freshness via `make i18n-check`, catching forgotten recompilation
- Merge conflicts on `.mo` files are resolved by recompiling from the merged `.po`
