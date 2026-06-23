# AI Usage in Revel

Revel openly uses AI-assisted coding, but stays away from vibe coding like pestilence (except for bits of the frontend, let's be honest).

You are welcome to contribute to Revel with PRs or Issues created with the help of AI, as
long as you follow the workflow described here. These are the same guardrails we use
internally. They are not optional: they are what keeps AI a catalyst instead of a
slop generator.

If you take one thing away: **AI accelerates the work, it does not own it.** Every line that
lands in `main` is understood, reviewed, and defended by a human. No exceptions.

---

## The beginning

Revel was bootstrapped in 2025 without much use of AI. The core architecture, API patterns,
conventions, and best practices came entirely from the human brain of the founder and core
maintainer **[Biagio Distefano](https://github.com/biagiodistefano)**.

The very first things set up (and still evolving actively) are the guardrails: QA tooling and CI.
The guardrails came before the LLMs, as LLMs without guardrails are a guaranteed headache that we don't want to have.

**TL;DR:** running `make check` and `make test` before shipping a PR takes you a long way.

---

## Guardrails & QA posture

The [Makefile](./Makefile) is the contract. AI can write a lot of plausible-looking code quickly; the
toolchain is what separates plausible from correct. Everything below runs locally and in CI,
and a red check blocks a merge.

`make check` bundles the fast static gate:

- **`ruff format` + `ruff lint`** — one canonical style, auto-fixed. No bikeshedding, no
  AI inventing its own formatting.
- **`mypy --strict`** (with the Django plugin) — full type coverage on every signature,
  including tests and fixtures. Catches the confidently-wrong type errors that AI loves to
  produce.
- **`migration-check`** — fails if models changed without a matching migration.
- **`i18n-check`** — fails if translations are stale.
- **`file-length`** — hard 1000-line-per-file cap, so nothing grows into an unreviewable blob.

`make test` runs the suite in parallel with **90%+ branch coverage required**.

Security and supply-chain gates run alongside:

- **`make bandit`** — static security analysis of the source.
- **`make licensecheck`** — fails on a license incompatible with our MIT distribution.
- **`make audit`** (`pip-audit`) — fails on known CVEs in the resolved dependency graph.

The point of all this is: a contributor (whether human or LLM) gets fast, unambiguous
feedback, and AI (and human) slop hits a wall long before it reaches review.

However! **green checks are the bare minimum**: they don't guarantee sound logic or architectural quality (nor that your PR will be accepted and merged).

---

## The workflow

The usual path from idea to merged PR:

1. **Input idea**: a shower thought, a GitHub issue, or a contribution from a community member.
2. **Validate against the philosophy**: does this make sense as a product and business
   decision for Revel? If not, it stops here.
3. **Validate against the codebase**: match the idea to the current architecture and confirm
   it is actually feasible to build the way it's imagined.
4. **Human thinks extra hard.** This step is not skippable and never delegated.
5. **AI-powered brainstorming**: using Claude's `superpowers` brainstorming skill to explore
   intent, requirements, and design before any code exists.
6. **Scope the GitHub issue(s)**: write down what's actually being built.
7. **Fresh session, pick up the issue, re-validate it.** A clean context forces the idea to
   stand on its own rather than on conversation momentum.
8. **Write the spec**: use the superpower to draft it, review, iterate until happy.
9. **Write the plan**: same loop: draft, review, iterate until happy.
10. **Unleash the subagents** to implement against the approved plan.
11. **Open the PR.**

Human judgment sits in front of the first line of code.
The AI acts only after a human has decided the thing is worth
building and how it should fit.

The `brainstorm → spec → plan → implement` loop exists precisely
so that no one is "just vibing" code into the repo.

---

## Tests are not optional

CI catches most things, but the responsibility is the contributor's.

- New or changed behaviour **must** come with corresponding test coverage.
- PRs that introduce or modify behaviour without tests will be **rejected**.
- The 90% branch-coverage floor is enforced by CI, but coverage is a measure, not a goal:
  tests must assert real business behaviour, not just execute lines.

The `test-engineer` agent (see below) helps write thorough tests, including the error and
edge cases, but **you** own the assertions.

---

## PR review has three layers

No single reviewer (human or machine) is trusted alone. Every PR gets reviewed three ways:

1. **CodeRabbit** runs automatically on GitHub (and shout-out to [CodeRabbit](https://www.coderabbit.ai/) for supporting FOSS).
2. **Claude review** — the `/code-review` superpower and/or the `pr-reviewer` agent.
3. **Your own independent review** — read the diff yourself, in full, with your own eyes.

Then **merge the findings, re-validate them, and address them.** Postpone as few fixes as
possible. Anything genuinely deferred must be tracked as a GitHub issue and **NEVER** silently
dropped. "The AI said it was fine" is not a review.

---

## Additional tooling

Revel ships with purpose-built agents, slash commands, and skills under [`.claude`](.claude).
These encode our conventions so AI assistance stays aligned with the project instead of
fighting it.

### Agents

- **`pr-reviewer`**: holistic PR review against Django/Python best practices, project
  patterns, migration safety, and test coverage.
- **`test-engineer`**: writes comprehensive tests with meaningful business assertions,
  covering success and error paths.
- **`vuln-analyst`** / **`vuln-verifier`**: region-scoped security hunter plus an independent
  adjudicator that confirms each finding is real and present-day. Gets unleashed regularly.
- **`tech-debt-assessor`** / **`tech-debt-verifier`**: region-scoped tech-debt hunter plus an
  adjudicator that scores findings by confidence. Gets unleashed regularly.

### Slash commands

- **`/vuln-scan`** — multi-agent security sweep: fans out region-scoped hunters, verifies
  findings, and reports.
- **`/tech-debt`** — multi-agent technical-debt assessment: fans out hunters + specialists,
  verifies, scores repo health, and writes a report.
- **`/logs`** — read production logs from Loki (via Grafana) by service, level, request/trace
  id, or time window (only available to maintainers with access to the prod server; PII is obfuscated).
- **`/update-changelog`** — update `CHANGELOG.md` from git tags, merged PRs, and actual diffs.
- **`/whats-new`** — draft a community "What's New" update across backend, frontend, and infra.

### Skills

- **`loki-logs`** — investigate production behaviour from real Loki logs (500s, stuck Celery
  tasks, tracing a request across services).
- **`silk-debug`** — analyze Django Silk profiling data to find slow requests and N+1 queries.
- **`updating-deps`** — scan for outdated/unused dependencies and update them safely with UV.
- **`update-changelog`** / **`whats-new`** — the skill counterparts to the commands above.

---

Use AI to go faster. Do not use it to think less.

## Fair warning

If you contribute with PRs or Issues that are mindless AI slop,
you will be banned from contributing without hesitation and without chance of appeal.
