# Security Policy

Revel is an event-management and ticketing platform that handles personal data,
payments, and access control. We take security seriously and run layered,
mostly-automated controls in CI and on a schedule. This document describes that
posture and how to report a vulnerability.

## Reporting a vulnerability

**Please report security issues privately — do not open a public issue or PR.**

Use GitHub's private vulnerability reporting:

1. Go to the **Security** tab of this repository →
   **[Report a vulnerability](https://github.com/letsrevel/revel-backend/security/advisories/new)**.
2. Describe the issue: affected endpoint/component, impact, and reproduction
   steps or a proof of concept. Include the commit/version if known.
3. We'll acknowledge within a few business days, work with you privately on a
   fix, and credit you in the published advisory unless you prefer otherwise.

This opens a **private draft security advisory** visible only to maintainers —
the report is never exposed publicly until we publish a coordinated advisory.

Please give us reasonable time to remediate before any public disclosure, and
avoid privacy violations, data destruction, or service degradation while
testing.

## What's in CI

Every pull request and push runs through these gates (see `.github/workflows/`):

| Control | Tool | Workflow | Trigger |
|---|---|---|---|
| **SAST** (static analysis of Python) | `bandit -ll -ii` | `bandit.yaml` | push/PR touching `src/**/*.py` or deps |
| **Dependency CVE scan** | `pip-audit --strict` (via `make audit`) | `deps.yaml` | push/PR touching `pyproject.toml`, `uv.lock`, `Makefile` |
| **License compliance** (blocks copyleft / source-available licenses) | `licensecheck` | `deps.yaml` | same as above |
| **Strict type checking** | `mypy --strict --extra-checks --warn-unreachable` | `test.yaml` | every push/PR |
| **Lint & format** | `ruff` | `test.yaml` | every push/PR |
| **Migration & i18n integrity** | `manage.py makemigrations --check`, i18n compile check | `test.yaml` | every push/PR |
| **Test suite** | `pytest` with **90% branch-coverage gate** (4 shards) | `test.yaml` | every push/PR |

Type strictness and the coverage gate are part of our security posture: they
keep the auth, permission, and payment paths from regressing silently.

## Scheduled jobs

- **Nightly dependency audit** (`nightly-audit.yml`, `03:17 UTC`) re-runs
  `pip-audit` and `licensecheck` against `main`, so newly-disclosed advisories
  surface within ~24h even without a PR. On failure it opens — or bumps — a
  tracking issue labelled `security` / `dependencies`.
- **Periodic DAST** — we run [OWASP ZAP](https://www.zaproxy.org/) passive
  baseline scans against the public (unauthenticated) frontend pages from the
  separate `pentesting/` repository. These are safe to run against production
  and surface header, cookie, and information-disclosure regressions.

## AI-assisted security review

The repository ships an adversarial, agent-based review harness under
`.claude/` (run by maintainers during development, not in CI):

- **`/vuln-scan`** — fans out region-scoped hunters across the codebase in
  parallel, then runs an independent verification pass to adjudicate each
  candidate finding and filter false positives.
- **`vuln-analyst`** — the hunter agent; an adversarial application-security
  reviewer specialized in this Django/Django-Ninja, multi-tenant, Celery
  stack. Can also be invoked standalone for an ad-hoc review of a specific
  endpoint, service, or app.
- **`vuln-verifier`** — re-reads each candidate finding independently and
  returns a verdict with a confidence score before anything is reported.

## Dependency CVE suppression policy

When `pip-audit` flags a CVE we cannot immediately fix, we follow the triage in
[`docs/runbooks/dependency-audit.md`](docs/runbooks/dependency-audit.md):

- **Bump** whenever a fixed version is reachable within our constraints
  (preferred).
- **Suppress** only when the advisory is withdrawn, not exploitable in our
  context, or has no reachable fix. Each suppression lives in the `audit` target
  of the `Makefile` with an inline rationale, is **tracked privately as a draft
  GitHub security advisory**, and is **re-reviewed quarterly**.

Suppressing a CVE never hides it: the rationale and the exit condition (what has
to change to drop the suppression) are recorded alongside the `--ignore-vuln`
flag and in the linked advisory.

## Supported versions

Revel is deployed as a continuously-released service; security fixes land on
`main` and are deployed from there. There is no extended support for older
tags — please report issues against the current `main`.
