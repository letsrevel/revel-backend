# ADR-0010: Dependency license & vulnerability checks

## Status

Accepted

## Context

`bandit` covers our own source, but nothing scanned what `uv.lock` actually
resolved to. Two classes of risk were unguarded:

- **Licenses** — an accidental copyleft pull (GPL, AGPL, SSPL, BUSL, Commons
  Clause) in a transitive would silently violate our MIT distribution stance.
- **CVEs** — advisories affecting pinned deps could linger for days between
  PRs, since the cadence at which we touch `uv.lock` is irregular.

We wanted an automated net for both, catching regressions at merge and also
surfacing newly-disclosed advisories on `main` within ~24h without requiring
a PR to happen first.

Alternatives considered:

1. **Snyk / Dependabot alerts only** — GitHub already runs Dependabot; but
   alerts are silent until someone reads Security tab, and the license side
   isn't covered at all.
2. **Single all-in-one SCA tool (e.g. OSV-Scanner, Trivy)** — broader scope
   but heavier CI cost and less transparent config. For a Python-only
   backend, `pip-audit` (PyPA) + `licensecheck` (FHPythonUtils) are
   lighter-weight and already `pyproject.toml`-native.
3. **Run checks inside `make check`** — rejected. That target is the tight
   offline loop; license/audit checks hit the network. Keeping them
   separate preserves the fast inner loop.

## Decision

Add two tools, two workflows, one runbook:

- **`licensecheck`** and **`pip-audit`** in the `dev` dependency group.
- Config for `licensecheck` lives in `[tool.licensecheck]` in `pyproject.toml`:
  `zero = true` (required for CI to actually fail on incompatible licenses —
  without it, the tool exits 0), `groups = ["dev"]` (walks the PEP 735 dev
  group), and a **hybrid deny-list** in `fail_licenses` for known copyleft /
  source-available families. Unknown SPDX identifiers warn but don't fail —
  this avoids churn on every obscure-but-permissive license that appears
  transitively.
- `pip-audit` runs via `make audit`, which invokes
  `uv export --no-emit-project | pip-audit -r … --no-deps`. Auditing a
  requirements file rather than the project avoids the pip-audit editable-
  project resolution error our `[build-system]`-enabled package triggers.
- Makefile targets `licensecheck`, `audit`, `deps-check` — not added to
  `make check`.
- `.github/workflows/deps.yaml` — runs both on any PR or push to `main` that
  touches `pyproject.toml`, `uv.lock`, or the workflow itself. Two jobs so
  PR status shows each failure distinctly.
- `.github/workflows/nightly-audit.yml` — re-runs both against `main` at
  03:17 UTC daily. On failure, each job opens-or-bumps an issue tagged
  `security` + `dependencies`, with distinct title slugs so the two checks
  dedupe independently.
- Operational triage lives in `docs/runbooks/dependency-audit.md`.

## Consequences

**Positive:**

- Copyleft regressions fail fast at merge, and newly-disclosed CVEs surface
  within ~24h on `main`.
- Suppressions (`--ignore-vuln`, `ignore_packages`) are explicit and
  commented next to the code that ignores them, not hidden in a dashboard.
- `make check` stays offline and fast.

**Negative:**

- Two more CI workflows to maintain.
- `licensecheck`'s matrix is MIT-compatibility-driven; permissive-but-non-MIT
  licenses (ZPL) need `ignore_packages` entries even though they're
  compatible. Manageable — each entry carries a rationale.
- `pip-audit` + our editable project needed a workaround
  (`uv export --no-emit-project`) documented in `Makefile`.

**Neutral:**

- Frontend dep scanning is out of scope for this ADR — `revel-frontend/` has
  its own story (or doesn't yet).
- If we later want SBOMs or license-allow-list (not deny-list) policy, this
  can be revisited without unwinding.

## References

- Runbook: `docs/runbooks/dependency-audit.md`
- Workflows: `.github/workflows/deps.yaml`, `.github/workflows/nightly-audit.yml`
- Tools: [licensecheck](https://github.com/FHPythonUtils/LicenseCheck),
  [pip-audit](https://github.com/pypa/pip-audit)
