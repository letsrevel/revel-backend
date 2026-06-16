# ADR-0011: Build Compiled Translations (.mo), Don't Commit Them

## Status

Accepted (supersedes [ADR-0006](0006-mo-files-in-git.md))

## Context

[ADR-0006](0006-mo-files-in-git.md) decided to **commit compiled `.mo` files** to
git and verify their freshness in CI by recompiling and diffing. The stated goal was
twofold: avoid a `gettext` dependency at build/deploy time, and "make sure there are
no missing translations".

In practice that second goal was not actually met by the mechanism. The freshness
check only verifies that `.mo` was recompiled after a `.po` edit — it says nothing
about whether the catalog is **complete**. A developer can add a `_()`-wrapped string
in code and never extract it (missing key), or leave a `msgstr` empty or `fuzzy`
(untranslated key), and a `.mo`-freshness check stays green. "No missing
translations" is a property of the **keys** in the `.po` source, not of whether the
binary was compiled.

Committing `.mo` also had real costs:

- **Binary churn in git** that nobody reviews — a recompile rewrites the whole file.
- **`make makemessages` noise**: gettext stamps a `POT-Creation-Date` header on every
  run, so every extraction produced a diff even when no strings changed. The three
  catalogs drifted to inconsistent dates as languages were added at different times.
- The freshness check compiled on every `make check`, mutating the working tree.

`gettext` is already installed in CI, and the only environment that genuinely needs
compiled catalogs at runtime is the Docker image — which can compile them itself.

## Decision

**Do not commit `.mo` files.** Compile them where they are needed and enforce
translation *completeness* on the `.po` sources instead.

1. **`.mo` is git-ignored** (`*.mo`). The 3 committed binaries were removed.
2. **The Docker image compiles** `manage.py compilemessages` in the builder stage
   (`gettext` added there). The runtime stage receives the compiled `src` via `COPY`,
   so runtime needs no `gettext`.
3. **Tests compile first**: the i18n tests assert translated strings, so
   `make test` (and the CI test job) run `compilemessages` before pytest. `gettext`
   was already a dev/CI dependency, so this adds no new requirement.
4. **`make makemessages` is deterministic**: it strips the `POT-Creation-Date` header
   after extraction (combined with the existing `--no-location`), so diffs appear only
   on real string changes.
5. **A static QA gate replaces the freshness check** — `scripts/check_translations.py`,
   run in `make check` and CI, enforces two things on the `.po` sources:
   - **Keys extracted**: re-runs `makemessages` and fails if any code string is
     *missing* from the catalog. It compares `msgid` *sets* (not raw text) and
     restores the files afterwards, so it is immune to gettext version reflow and
     never leaves working-tree changes.
   - **Keys translated**: fails on any `fuzzy` or empty `msgstr` entry.

## Consequences

**Positive:**

- The gate actually enforces "no missing translations" — both missing keys and
  untranslated strings — which is what we wanted all along.
- No binary `.mo` churn in git; no `POT-Creation-Date` noise on extraction.
- The extraction check is version-independent, so CI/dev gettext mismatches don't
  cause spurious failures.

**Negative:**

- The Docker build and the test runner now depend on `gettext` (already present in
  CI; required of developers, who already had it for i18n work).
- Every environment that serves translated content must compile catalogs once. This
  is the dependency ADR-0006 tried to avoid; we accept it as localized to the image
  build and test setup.

**Neutral:**

- `make compilemessages` remains the way to build catalogs locally; it is now a
  prerequisite of the `test*` targets.
