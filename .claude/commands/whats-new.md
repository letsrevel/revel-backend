---
description: Draft a "What's New" Discord community update across backend, frontend & infra for a time window
argument-hint: "[window: number + w|m, e.g. 1w, 2m, 6w — default 1m]"
---

Invoke the `whats-new` skill via the Skill tool and follow its workflow to produce a Discord-ready
"What's New" community update and archive it on the docs site.

Window from the user (may be empty): $ARGUMENTS

- The window is `number + w|m` (e.g. `1w`, `2m`, `6w`). If `$ARGUMENTS` is empty or unrecognized,
  default to the **last month** (`1m`).
- Research changelog-first (backend `CHANGELOG.md`, frontend `CHANGELOG.md` if it exists), digging
  into git/PRs only for gaps and for repos without a changelog (infra; frontend until it has one).
- Do not commit — print the Discord draft, update `docs/whats-new/index.md`, then offer to commit.
