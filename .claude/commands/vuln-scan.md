---
description: Multi-agent security vulnerability sweep — fans out region-scoped hunters, verifies findings, and reports
argument-hint: "[scope: full (default) | <app/path> | diff] [--fast | --deep] [--report <file>]"
allowed-tools: Bash(git ls-files:*), Bash(git diff:*), Bash(git merge-base:*), Bash(git log:*), Bash(git rev-parse:*)
disable-model-invocation: true
---

You are the **orchestrator** for a multi-agent security review of the Revel backend. You run in the main session (you can fan out parallel sub-agents; the workers cannot). Your job is to partition the work, dispatch `vuln-analyst` **hunters** and `vuln-verifier` **verifiers**, then consolidate a precise, false-positive-free report. Follow these phases **in order**. Make a task list first.

User arguments (may be empty): **$ARGUMENTS**

---

## Phase 0 — Parse args & triage (you do this inline; no agent)

**Parse `$ARGUMENTS`:**
- **scope** (first non-flag token):
  - empty or `full` → **full-codebase sweep** (default): dispatch a hunter for every region in the map below.
  - an app or path (e.g. `accounts`, `events/service`, `src/wallet`) → **single-region**: dispatch one hunter scoped to that path (pick its model tier from the map, or by risk if it's a sub-path).
  - `diff` → **branch-diff scope**: run `git merge-base main HEAD` then `git diff --name-only $(git merge-base main HEAD) HEAD -- src/`. Map each changed file to its region; dispatch hunters **only** for regions with changes, and instruct each to focus on the changed files **plus** the dependencies they trace into.
- **flags:**
  - `--fast` → override all hunter models to **sonnet**.
  - `--deep` → override all hunter models to **opus**.
  - `--report <file>` → in Phase 5, also write the full report to `<file>` (markdown).
  - default (no model flag) → use the **tiered** models in the region map.

**Reconcile the region map with the live tree** (catches newly-added apps/modules):
- Run `git ls-files 'src/*/apps.py'` to list Django apps, and `git ls-files 'src/events/*/__init__.py'` for events sub-packages.
- If an app/module exists in the tree but is **not** covered by any region's globs below, create an ad-hoc region for it: globs `src/<name>/**`, default model **sonnet**, surfaces = the full framework, risk MEDIUM. Note in the final report that an unmapped module was discovered (so the map can be updated).

**Region map** (priority-ordered; `model` is the default/tiered choice, overridden by `--fast`/`--deep`):

| Region | Globs | Focus surfaces | Risk | Model |
|---|---|---|---|---|
| accounts | `src/accounts/**` | Auth & JWT, registration/verification, GDPR export/erasure, impersonation, mass-assignment on user fields | CRITICAL | opus |
| events-service | `src/events/service/**` | Ticket/payment/checkout logic, eligibility, invitations, **race conditions** (`select_for_update`, `get_or_create_with_race_protection`), state machines, `transaction.on_commit` correctness | CRITICAL | opus |
| events-controllers | `src/events/controllers/**` | Authz/BOLA/IDOR, tenant isolation, optional-auth abuse, throttling on mutations, permission-class chain | HIGH | opus |
| events-models | `src/events/models/**`, `src/events/constants/**` | Permission model (`has_org_permission`, `PermissionMap`), state-machine constraints, model `clean()`/`save()` validation | HIGH | opus |
| events-schema | `src/events/schema/**` | Mass assignment, sensitive-field exposure in input/output schemas | MEDIUM | sonnet |
| questionnaires | `src/questionnaires/**` | Access-gate bypass, LLM prompt injection (concrete new bypass only), submission/ownership validation | HIGH | opus |
| common | `src/common/**` | Base models, auth utilities, file handling (MIME/ClamAV/EXIF), exception handlers, `get_or_create_with_race_protection` | HIGH | opus |
| notifications | `src/notifications/**` | Unsubscribe-token handling, digest/eligibility logic, PII in email payloads | MEDIUM | sonnet |
| wallet | `src/wallet/**` | `.pkpass` forgery/tampering, path traversal, ticket-scoping of pass generation | MEDIUM | sonnet |
| telegram | `src/telegram/**` | Bot FSM state abuse, token/secret handling, command authorization | MEDIUM | sonnet |
| events-admin | `src/events/admin/**`, `src/events/utils/**`, `src/events/management/**` | Admin-gated operations, management-command safety | LOW | sonnet |
| geo+api+polls | `src/geo/**`, `src/api/**`, `src/polls/**` | IP geolocation trust, global rate-limit/exception config, poll authz | LOW | sonnet |

For a single-region scope that is a sub-path not listed (e.g. `events/templatetags`), use globs = that path, surfaces = full framework, model = sonnet (or per `--deep`).

Build the **dispatch plan**: a list of `(region, globs, focus surfaces, model)`. Show it to the user as a short table before dispatching.

---

## Phase 1 — Fan out HUNTERS (parallel, in waves)

Dispatch one `vuln-analyst` agent per planned region using the **Agent tool** with `subagent_type: "vuln-analyst"` and the region's `model`. **Cap concurrency at ~6 agents per wave** — send the first wave (highest-risk regions first), wait for it, then the next, until all regions are done. Launch the agents in a wave as multiple tool calls in a **single message** so they run concurrently.

Use this prompt template for each hunter (fill the placeholders):

> **Mode A — Scoped Hunter.**
> REGION: `<region name>`
> GLOBS (the only files you own — read only these plus dependencies you must trace): `<globs>`
> FOCUS SURFACES (lead with these, but report any egregious issue): `<focus surfaces>`
> SCOPE NOTE: `<for diff scope: "Focus on these changed files and what they touch: <files>". For full/region scope: "Review the whole region.">`
> Consult your MEMORY.md and do NOT re-flag confirmed-secure patterns. Output structured `### CANDIDATE` blocks exactly as specified in your Output Format (Mode A), with an honest `hunter_confidence`. Do not run tests or mutating commands.

Collect every CANDIDATE block from every hunter into a single list. Preserve each candidate's `id` (they're region-prefixed, so unique).

---

## Phase 2 — Dedup (you do this inline)

Hunters trace into shared dependencies, so the same flaw can be reported by two regions. Merge candidates that point at the **same root cause** (same code location/control flow, even if titled differently): keep the higher-severity / higher-`hunter_confidence` one, and note the merge. Do **not** merge distinct issues that merely sit in the same file.

If there are **zero** candidates after dedup, skip to Phase 5 and report a clean sweep.

---

## Phase 3 — Fan out VERIFIERS (parallel)

For each surviving candidate, dispatch a `vuln-verifier` agent via the **Agent tool** with `subagent_type: "vuln-verifier"` (default model sonnet; you may use opus for CRITICAL-severity candidates). One verifier per candidate; if there are many trivial/low candidates you may batch up to 3 closely-related ones into a single verifier call, but ask it to emit one `### VERDICT` block per `id`. Cap concurrency at ~6 per wave.

Pass each verifier the **complete candidate block verbatim** (all fields). It will independently re-read the code and return a `### VERDICT` with `verdict`, `confidence`, `severity`, and `reasoning`.

Collect all verdicts and join them back to their candidates by `id`.

---

## Phase 4 — Filter (severity-aware threshold; you do this inline)

Using the verifier's **corrected `severity`** and **`confidence`**, keep a finding only if:

- **CRITICAL or HIGH** → confidence **≥ 60**
- **MEDIUM** → confidence **≥ 75**
- **LOW or INFORMATIONAL** → confidence **≥ 90**

Drop everything else (and any `verdict: FALSE_POSITIVE` regardless of score). Count what you dropped — it goes in the summary as "screened out". Sort surviving findings by severity (CRITICAL→LOW), then confidence descending.

---

## Phase 5 — Report & consolidate memory

**Report to the user** in the session (and to `--report <file>` if given) using this format:

```
# Security sweep — <scope> — <date>

**Posture:** <one line>. <X> finding(s) reported · <Y> candidate(s) screened out · <Z> region(s) reviewed (<models used>).

## Findings
### [SEVERITY] <title>   (confidence <N>)
- **Category:** <category>
- **Location:** <path:lines>
- **Description:** ...
- **Attack scenario:** ...
- **Impact:** ...
- **Recommendation:** ...
- **Verifier note:** <decisive reasoning>

(repeat per finding; if none: "No findings met the reporting threshold.")

## Coverage
<table: region · model · candidates · confirmed · screened out>

## Notes
<unmapped modules discovered, regions skipped, anything the user should know>
```

**Then consolidate memory (you are the sole writer):**
- Read `.claude/agent-memory/vuln-analyst/MEMORY.md`.
- Propose updates to the user first, then write them in **one** edit:
  - Add genuinely new **confirmed-secure patterns** surfaced by hunters (from their SUMMARY notes) that will prevent future false positives.
  - Add or update **historically risky areas** where real findings were confirmed.
  - Replace the dated **scan outcome** line with this sweep's result and date.
- Keep `MEMORY.md` concise (it loads into every hunter's prompt; content past ~200 lines is truncated). Do not record session-specific or speculative notes.

## Guardrails

- **Never run tests or mutating commands.** This sweep is static analysis only; let the user run anything they want to confirm.
- **Precision over volume.** A clean report stating "no exploitable findings" is a success, not a failure. Do not pad.
- **Respect MEMORY.md** — if a candidate matches a confirmed-secure pattern, it should not survive verification; if one slips through, drop it and note why.
