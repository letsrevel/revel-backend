# Dependency audit runbook

Two checks guard the `revel-backend` dependency graph:

- **`pip-audit`** — queries the PyPI Advisory Database and fails on any known CVE
  in the resolved tree.
- **`licensecheck`** — walks `pyproject.toml` + `uv.lock` and fails on
  copyleft / source-available licenses (GPL, AGPL, SSPL, BUSL, Commons Clause)
  entering the tree.

Both run in two contexts:

- **PR / push to `main`** — `.github/workflows/deps.yaml` runs both on any change
  that touches `pyproject.toml`, `uv.lock`, or the workflow itself.
- **Nightly, `03:17 UTC`** — `.github/workflows/nightly-audit.yml` re-runs both
  against `main` and opens-or-bumps a GitHub issue (labels: `security`,
  `dependencies`) on failure, so newly-disclosed advisories surface within ~24h
  without a PR.

## Local reproduction

```bash
make deps-check    # runs both
make licensecheck  # licenses only
make audit         # pip-audit only
```

Neither target is part of `make check` — `make check` is kept fast and offline;
dependency checks hit the network and live outside that loop.

## Triage: nightly issue filed

### `pip-audit`

1. Open the linked Actions run from the issue body to see the advisory ID
   (`GHSA-xxxx-xxxx-xxxx` / `PYSEC-…`) and affected package/version.
2. Read the advisory. Decide:
   - **Bump** — preferred when a fixed version exists. `uv lock --upgrade-package <pkg>`
     (or `uv add <pkg>@<version>` for a direct dep), verify `make test`, open a PR.
   - **Suppress** — only if the advisory is withdrawn, not actually exploitable
     in our context, or there is no fix yet. Add `--ignore-vuln <ID>` to the
     `audit` target in the Makefile with a comment explaining the rationale.
     The workflow invocations call `make audit`, so the suppression flows
     through to CI automatically. Re-review suppressions quarterly.
3. Close the tracker issue when the PR lands (or link the suppressed-vuln PR).

Current grandfathered suppressions live at the top of the `audit` target in
`Makefile` alongside their rationale.

### Gotcha: `-r pyproject.toml`

The `licensecheck` target passes `-r pyproject.toml` explicitly. Do not drop
that flag. When licensecheck runs without it and stdin is not a tty (i.e.
`make`, CI, any pipeline), it treats `__stdin__` as the requirements source
and silently falls back to a partial graph. With the flag, the uv resolver
walks `[project.dependencies]` plus the configured `[tool.licensecheck].groups`
and you get the full 280+ package tree.

### Gotcha: `pip-audit --disable-pip` in CI

The `audit` target passes `--disable-pip` alongside `--no-deps`. `pip-audit -r`
always spawns a fresh venv and runs `python -m ensurepip` inside it; uv's
managed Python doesn't ship `ensurepip`, so the step aborts with exit 127 on
CI runners that use `setup-uv`. `--disable-pip` skips the bootstrap entirely
(safe because with `--no-deps` we don't need pip to resolve). Locally this
may mask if your system Python has `ensurepip`, but CI always needs it.

### `licensecheck`

1. Open the Actions run and identify which package triggered the fail (the tool
   prints the package, its license, and why it matched `fail_licenses`).
2. Decide:
   - **Replace / bump** — preferred. Swap for a permissively-licensed alternative
     or upgrade to a version that has been relicensed.
   - **Grandfather** — if the package is load-bearing and no alternative exists,
     add it to `[tool.licensecheck].ignore_packages` in `pyproject.toml` with an
     inline comment explaining why the license is acceptable in our deployment
     model (e.g. `# <pkg>: AGPL, but used only as a CLI-separate process, not linked`).
3. Close the tracker issue when the PR lands.

## Label taxonomy

The nightly workflow applies these labels when opening or bumping an issue:

- `security` — advisory / compliance impact
- `dependencies` — third-party code

Both labels are required to exist in the repo for the issue-creation step to
succeed.

## Policy summary

- **Fail list is copyleft-only.** Permissive unknowns (PSF-2.0, ISC variants,
  BSD-3-Clause-Clear, etc.) warn but don't fail (`zero = true` with no
  `only_licenses` catch-all). This is deliberate — maintaining an explicit
  allow-list of every permissive SPDX ID generates churn without safety gain.
- **Grandfathering is explicit.** Every entry in `ignore_packages` /
  `--ignore-vuln` carries a rationale comment. Drive-by additions without a
  comment fail review.
- **Frontend is out of scope.** `revel-frontend/` dep scanning is handled (or
  not) separately; this runbook covers `revel-backend/` only.
