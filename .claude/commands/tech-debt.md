---
description: Multi-agent technical-debt assessment — fans out region-scoped + specialist hunters, verifies findings, scores repo health, and writes a report
argument-hint: "[scope: full (default) | <app/path> | diff] [--fast | --deep] [--report <file>]"
allowed-tools: Bash(git ls-files:*), Bash(git diff:*), Bash(git merge-base:*), Bash(git log:*), Bash(git rev-parse:*), Bash(gh issue list:*), Bash(grep:*), Bash(rg:*), Bash(find:*), Bash(wc:*), Bash(cloc:*), Bash(date:*)
disable-model-invocation: true
---

You are the **orchestrator** for a multi-agent technical-debt assessment of the Revel backend. You run in the main session (you can fan out parallel sub-agents; the workers cannot). Your job is to partition the work, dispatch `tech-debt-assessor` **hunters** and `tech-debt-verifier` **verifiers**, then consolidate a precise, scored, false-positive-free report and **write it to disk by default**. Follow these phases **in order**. Make a task list first.

User arguments (may be empty): **$ARGUMENTS**

---

## Phase 0 — Parse args, gather metrics & triage (you do this inline; no agent)

**Parse `$ARGUMENTS`:**
- **scope** (first non-flag token):
  - empty or `full` → **full-repo sweep** (default): dispatch a hunter for every region in the map below **plus** all specialist hunters.
  - an app or path (e.g. `accounts`, `events/service`, `src/wallet`) → **single-region**: dispatch one hunter scoped to that path (pick its model tier from the map, or by size/risk if it's a sub-path). Skip the specialists unless the path is repo-wide.
  - `diff` → **branch-diff scope**: run `git merge-base main HEAD` then `git diff --name-only $(git merge-base main HEAD) HEAD -- src/`. Map each changed file to its region; dispatch hunters **only** for regions with changes, instructing each to focus on the changed files **plus** the dependencies they trace into. Also run the `consistency-standards` specialist if more than one region changed.
- **flags:**
  - `--fast` → override all hunter models to **sonnet**.
  - `--deep` → override all hunter models to **opus**.
  - `--report <file>` → override the default output path (see Phase 5).
  - default (no model flag) → use the **tiered** models in the region/specialist maps.

**Gather cheap deterministic metrics inline** (these need no agent — do them with the allowed read-only Bash tools, and feed the results to Phase 5 so findings already tracked aren't reported as "new"):
- `date +%F` for the report date.
- Production LOC & file count: e.g. `git ls-files 'src/**/*.py' | grep -v migrations | grep -v tests | wc -l` and a `cloc src` if available (else `wc -l`).
- `grep -rEc "TODO|FIXME|HACK|XXX" src` (sum), `grep -rc "type: ignore" src`, `grep -rc "from typing import" src`, count of `# ponytail:` ceilings.
- Migration count per app: `git ls-files 'src/*/migrations/0*.py' | sed ...` (rough per-app tally).
- Files near the 1000-line cap: `git ls-files 'src/**/*.py' | xargs wc -l | sort -rn | head -20`.
- Open issues for context: `gh issue list --state open --limit 100` and `gh issue list --state open --label bug --limit 50`. Catalog by category. **Cross-reference in Phase 5** — don't report a known, tracked issue as a fresh discovery.

**Reconcile the region map with the live tree** (catches newly-added apps/modules):
- Run `git ls-files 'src/*/apps.py'` to list Django apps.
- If an app/module exists in the tree but is **not** covered by any region's globs below, create an ad-hoc region for it: globs `src/<name>/**`, default model **sonnet**, focus = full framework. Note in the final report that an unmapped module was discovered (so the map can be updated).

**Region map** (code regions; `model` is the default/tiered choice, overridden by `--fast`/`--deep`). FOCUS DIMENSIONS lead the hunter's attention but it reports any egregious debt it finds:

| Region | Globs | Focus dimensions | Model |
|---|---|---|---|
| accounts | `src/accounts/**` | Service/controller layering, auth-flow complexity, type safety, dead code, GDPR-flow duplication | sonnet |
| events-service | `src/events/service/**` | God functions, cyclomatic/cognitive complexity hotspots, duplication, service-pattern adherence, error handling | opus |
| events-controllers | `src/events/controllers/**` | Thin-controller adherence (business logic leaking into views), duplication across controllers, pattern consistency, permission-chain clarity | opus |
| events-models | `src/events/models/**`, `src/events/constants/**` | Fat models, model-design quality, indexes/constraints, dead model fields, models importing services | sonnet |
| events-schema | `src/events/schema/**` | ModelSchema conventions, enum/AwareDatetime adherence, schema duplication, re-export hygiene | sonnet |
| events-support | `src/events/admin/**`, `src/events/utils/**`, `src/events/management/**`, `src/events/tasks/**`, `src/events/templatetags/**` | Dead code, utils sprawl, management-command quality, Celery-task hygiene | sonnet |
| questionnaires | `src/questionnaires/**` | Complexity, LLM-eval code quality, layering, dead code | sonnet |
| common | `src/common/**` | Utility sprawl, base-model & mixin design, dead helpers, abstraction level | sonnet |
| notifications | `src/notifications/**` | Channel-abstraction quality, digest/preference complexity, duplication | sonnet |
| wallet+telegram | `src/wallet/**`, `src/telegram/**` | FSM complexity, dead code, file-handling clarity | sonnet |
| geo+polls+moderation | `src/geo/**`, `src/polls/**`, `src/moderation/**` | Layering, dead code, cross-app consistency | sonnet |
| api+config | `src/api/**`, `src/revel/**` | Settings organization, global config sprawl, exception-handler wiring | sonnet |

**Specialist map** (cross-cutting hunters; run on `full` scope, and selectively on `diff`). These read broadly but shallowly and own a single dimension across the whole tree:

| Specialist | Globs / scope | Focus | Model |
|---|---|---|---|
| dependency-health | `pyproject.toml`, `uv.lock` | Outdated/pinned deps, unused deps, dev-vs-prod separation, dependency risk | sonnet |
| consistency-standards | `src/**` (broad, shallow) | Pattern drift across apps, CLAUDE.md adherence, naming consistency, **evolution debt** (newer apps diverging from older patterns) | opus |
| test-quality | `src/**/tests/**`, `tests/**`, `conftest.py` | Coverage distribution, behavior-vs-implementation testing, over-mocking, factory/fixture usage, flaky-test & test-isolation patterns | sonnet |
| migration-debt | `src/*/migrations/**` | Migration accumulation, squash candidates, conflicting/data migrations, mutable-state-in-migration smells | sonnet |

For a single-region scope that is a sub-path not listed, use globs = that path, focus = full framework, model = sonnet (or per `--deep`).

Build the **dispatch plan**: a list of `(region/specialist, globs, focus dimensions, model)`. Show it to the user as a short table before dispatching.

---

## Phase 1 — Fan out HUNTERS (parallel, in waves)

Dispatch one `tech-debt-assessor` agent per planned region/specialist using the **Agent tool** with `subagent_type: "tech-debt-assessor"` and the assigned `model`. **Cap concurrency at ~6 agents per wave** — send the first wave (largest/highest-tier regions first), wait for it, then the next, until all are done. Launch the agents in a wave as multiple tool calls in a **single message** so they run concurrently.

Use this prompt template for each hunter (fill the placeholders):

> **Mode A — Scoped Hunter.**
> REGION: `<region/specialist name>`
> GLOBS (the files you own — read these plus dependencies you must trace; for dead-code candidates you may grep wider but do not deep-read other regions): `<globs>`
> FOCUS DIMENSIONS (lead with these, but report any egregious debt): `<focus dimensions>`
> SCOPE NOTE: `<for diff scope: "Focus on these changed files and what they touch: <files>". For full/region scope: "Review the whole region.">`
> OPEN ISSUES CONTEXT (do not re-report as new discoveries): `<short list of relevant open issue titles/numbers from Phase 0, or "none">`
> Consult your MEMORY.md and do NOT re-flag confirmed/known-accepted patterns. Output structured `### CANDIDATE` blocks and a `## REGION HEALTH` block exactly as specified in your Output Format (Mode A), with an honest `hunter_confidence`. Do not run tests or mutating commands.

Collect every CANDIDATE block **and** every REGION HEALTH block from every hunter. Preserve each candidate's `id` (they're region-prefixed, so unique). Keep the REGION HEALTH notes — you need them for scoring in Phase 5.

---

## Phase 2 — Dedup (you do this inline)

Hunters and specialists overlap (a complexity hotspot may be flagged by both the region hunter and `consistency-standards`). Merge candidates that point at the **same root cause** (same code location / same smell, even if titled differently): keep the higher-severity / higher-`hunter_confidence` one, and note the merge. Do **not** merge distinct issues that merely sit in the same file.

If there are **zero** candidates after dedup, skip to Phase 5 and report a clean, healthy sweep (you still compute the score and write the report).

---

## Phase 3 — Fan out VERIFIERS (parallel)

For each surviving candidate, dispatch a `tech-debt-verifier` agent via the **Agent tool** with `subagent_type: "tech-debt-verifier"` (default model sonnet; you may use opus for CRITICAL-severity or repo-wide-impact candidates). One verifier per candidate; you may batch up to 3 closely-related candidates into a single verifier call, but ask it to emit one `### VERDICT` block per `id`. Cap concurrency at ~6 per wave.

Pass each verifier the **complete candidate block verbatim** (all fields). It independently re-reads the code and returns a `### VERDICT` with `verdict`, `confidence`, `severity`, `category`, and `reasoning`. **Dead-code candidates get special rigor** — the verifier must prove no references exist anywhere (including dynamic `getattr`, signal receivers, URL patterns, template/`__init__` re-exports) before confirming.

Collect all verdicts and join them back to their candidates by `id`.

---

## Phase 4 — Filter (category-aware threshold; you do this inline)

Using the verifier's **corrected `severity`/`category`** and **`confidence`**, keep a finding only if:

- **Dead Code** (any severity) → confidence **≥ 85** (false positives here are costly and embarrassing).
- **CRITICAL or HIGH** → confidence **≥ 60**.
- **MEDIUM** → confidence **≥ 70**.
- **LOW** → confidence **≥ 85**.

Drop everything else (and any `verdict: FALSE_POSITIVE` regardless of score). Count what you dropped — it goes in the summary as "screened out". Sort surviving findings by severity (CRITICAL→LOW), then confidence descending.

---

## Phase 5 — Score, report & consolidate memory

**Compute the health score yourself** (hunters give per-region signals; only you have the aggregate). Use the REGION HEALTH notes + confirmed findings + Phase-0 metrics to fill the category breakdown, then derive the overall score.

Scoring calibration: **9-10** exemplary, near-zero debt · **7-8** healthy, minor manageable debt · **5-6** moderate debt, needs dedicated cleanup · **3-4** significant debt, velocity impacted · **1-2** critical, rewrites needed. A 7-8 is a healthy production codebase; be calibrated and honest. If a previous score exists in memory, report the **trend** (improved / held / regressed) and why.

**Write the report to disk by default.** Path: `<--report value>` if given, else `./<YYYY-MM-DD>-TECH-DEBT_REPORT.md` (date from Phase 0) at the repo root. Use the Write tool. Then also print a condensed summary in-session.

Report format:

```
# Technical Debt Assessment Report
**Date**: <YYYY-MM-DD>
**Repository**: revel-backend
**Version assessed**: <version> (HEAD <short-sha>)
**Scope**: <full | path | diff> · <N> region(s) + <M> specialist(s) reviewed (<models used>)
**Previous assessments**: <from memory, e.g. 8.2/10 (2026-06-23) · 8.0/10 (2026-05-15)>

## Executive Summary
<2-3 paragraphs: overall posture, what changed since last assessment, the single most actionable signal>

## Overall Health Score: X/10
<justify the score; state the trend vs the previous assessment>

## Scoring Breakdown
| Category | Score (1-10) | Notes |
|----------|-------------|-------|
| Architecture & Design | X | ... |
| Code Consistency | X | ... |
| Test Quality & Coverage | X | ... |
| Type Safety | X | ... |
| Dependency Health | X | ... |
| Documentation | X | ... |
| Complexity Management | X | ... |
| Dead Code | X | ... |
| Adherence to Project Standards | X | ... |
| Error Handling | X | ... |

## Detailed Findings
### Critical Issues (Must Fix)
### High Priority Tech Debt
### Medium Priority Improvements
### Low Priority / Nice-to-Have
### Acknowledged (Known Issues / In Progress)   <- cross-referenced GitHub issues
(each finding: title · category · location path:lines · description · impact · recommendation · effort (S/M/L) · confidence · verifier note. If none in a band, say so.)

## Positive Observations
<what the codebase does well — acknowledge good patterns and intentional tradeoffs>

## Tradeoff Analysis
<intentional tradeoffs and whether they're still appropriate>

## Top 10 Actionable Recommendations
<ordered by impact/effort ratio>

## App-by-App Summary
<one health line per region, from the REGION HEALTH notes>

## Dead Code Catalog
<confirmed dead code only, categorized: unused functions/methods, orphaned service fns, unused Celery tasks, commented-out blocks, unused fixtures, other — with file locations>

## Coverage
<table: region/specialist · model · candidates · confirmed · screened out>

## Metrics Summary
- Total production Python files / LOC
- Test files
- TODO/FIXME/HACK count · type: ignore count · from-typing-import violations · ponytail ceilings
- Files within 50 lines of the 1000-line cap
- Open GitHub issues: X (Y bugs, Z features, W debt)

## Notes
<unmapped modules discovered, regions skipped, anything the user should know>
```

**Then consolidate memory (you are the sole writer):**
- Read `.claude/agent-memory/tech-debt-assessor/MEMORY.md`.
- Propose updates to the user first, then write them in **one** edit:
  - Replace/append the dated **assessment outcome** line (score + date + version) so trend tracking works.
  - Add genuinely new **confirmed hotspots** (god classes, complexity, files near the cap) and **stable conventions** confirmed across regions.
  - Add genuinely new **confirmed-clean / known-accepted patterns** surfaced by hunters so future sweeps don't re-flag them.
- Keep `MEMORY.md` concise (it loads into every hunter's prompt; content past ~200 lines is truncated). Do not record session-specific or speculative notes.

## Guardrails

- **Never run tests, migrations, or mutating commands.** This assessment is static analysis only (plus the cheap read-only metrics in Phase 0). The only file you write is the report (and, after approval, memory).
- **Precision over volume.** A short report on a healthy codebase is a success, not a failure. Do not pad findings to fill sections.
- **Be fair.** Acknowledge good decisions and intentional tradeoffs — not everything is debt. Cross-reference open issues so tracked work isn't reported as a discovery.
- **Respect MEMORY.md** — if a candidate matches a confirmed-clean / known-accepted pattern, it should not survive verification; if one slips through, drop it and note why.
