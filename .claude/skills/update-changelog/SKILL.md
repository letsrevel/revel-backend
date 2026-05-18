---
name: update-changelog
description: Update CHANGELOG.md comprehensively based on git tags, merged PRs, and actual code diffs. Use when the user asks to "update the changelog", backfill missing versions, prepare a release, or reconcile [Unreleased] entries against shipped tags. Produces Keep-a-Changelog entries that describe user-facing impact, not implementation details.
allowed-tools: Bash(git:*), Bash(gh:*), Bash(grep:*), Bash(make:*), Read, Edit, Grep, Glob
---

# Update Changelog

Maintains `CHANGELOG.md` (Keep a Changelog format, semver) by reconciling it against git tags, merged PRs, and actual code diffs.

## Format conventions (this repo)

- Header: `## [X.Y.Z] - YYYY-MM-DD` for released versions, `## [Unreleased]` at the top.
- Section order: **Added**, **Changed**, **Fixed**, **Deprecated**, **Removed**, **Security**. Omit empty sections.
- Bullets describe **user/operator/API-consumer impact**, not internal refactors. Drop pure CI/test/lint commits unless they change observable behavior.
- Major features get a bold prefix: `- **VAT Support**: Complete VAT handling for ticket sales…`. Optional nested sub-bullets for sub-features.
- Bug fixes name the symptom that was fixed, not the patch ("`location_maps_url` max length increased from 200 to 2048 to accommodate long Google Maps URLs").
- Dates are the **tag date** (committer date of the tag), not today's date.

## Release model (this repo)

- `make release` runs `gh release create v$VERSION --generate-notes` using `version` from `pyproject.toml`.
- A "bump X.Y.Z" PR (often bundled with small fixes — e.g. `chore: bundled fixes (...) + bump 1.55.0`) lands on `main`, *then* `make release` is invoked.
- **CHANGELOG.md must be updated in the same commit that bumps `pyproject.toml`** — write the new `## [X.Y.Z] - YYYY-MM-DD` section before the bump PR is merged.

## Workflow

### 1. Establish state

```bash
git tag --sort=-v:refname | head -20             # latest tags
grep -E '^## \[' CHANGELOG.md | head -10         # versions present in changelog
grep '^version = ' pyproject.toml                 # pending version
```

Compare the three. Common situations:

- **Behind on tags** — CHANGELOG missing released versions → backfill each gap (step 2 per gap).
- **`[Unreleased]` populated, pyproject bumped** — promote `[Unreleased]` to `## [X.Y.Z] - <tag-date>` and start a fresh empty `[Unreleased]`.
- **New work since last tag** — add entries to `[Unreleased]` only.

### 2. Gather changes between two refs

For each version gap `vA..vB` (or `vLast..HEAD` for unreleased):

```bash
# 1. Tag date for the section header
git log -1 --format=%cs vB                          # YYYY-MM-DD

# 2. Merged PRs in the range (richest signal — PR titles are usually user-facing)
git log vA..vB --merges --format='%h %s' --first-parent

# 3. Full commit subjects on main (catch non-PR commits)
git log vA..vB --format='%h %s' --first-parent

# 4. For each non-trivial PR, read its body for the user-facing description
gh pr view <number> --json title,body,labels
```

Group commits by prefix (`feat:`, `fix:`, `refactor:`, `chore:`, `test:`, `docs:`, `ci:`) as a first pass — but do not trust the prefix alone (see step 3).

### 3. Verify impact from the diff

A commit message says *what the author intended*; the diff says *what actually changed*. For any entry where impact is unclear or the message is terse:

```bash
git show <sha> --stat                               # which files moved
git show <sha> -- <interesting-path>                # the actual change
git diff vA..vB -- src/<app>/                       # cumulative for a subsystem
```

Verify the user-facing surface:

- New endpoints / changed schemas → check `events/controllers/`, `accounts/controllers/`, `*/schema/`, `openapi.json` if regenerated.
- New models or migrations → check `*/models/`, `*/migrations/00XX_*.py` (squashed migrations matter for ops).
- Settings or env vars → check `src/revel/settings/`, `.env*`, `CLAUDE.md`.
- Background jobs / Celery → check `*/tasks.py`, `celery.py` beat schedule.
- Permissions / auth → check `events/controllers/permissions.py`, `common/auth/`.

If the diff is purely internal (renames, helper extraction, type hints, test reorg) and there is no observable change, **drop it** — even if the commit was `feat:`-prefixed.

### 4. Categorize

| Section | Use for |
|---------|---------|
| **Added** | New features, new endpoints, new models, new settings/flags, new notification channels, new file format support |
| **Changed** | Behavior changes to existing features, API contract changes (non-breaking), default-value changes, performance changes visible to users |
| **Fixed** | Bug fixes — describe the symptom |
| **Deprecated** | Features still working but slated for removal |
| **Removed** | Removed features, endpoints, settings |
| **Security** | Auth fixes, permission tightenings, vulnerability patches (do not name the CVE if not public) |

Bundle related commits into one bullet. A feature that spans 5 PRs becomes one bold-prefixed bullet, optionally with sub-bullets per sub-feature.

### 5. Write entries

Style examples lifted from this repo's existing entries — match the voice:

```markdown
### Added
- **Referral System — Full Launch**: Complete referral program with Stripe auto-payouts
  - `UserBillingProfile` model for referrer billing details (VAT ID, address, self-billing agreement)
  - Monthly payout calculation task aggregating net platform fees per referral
- VAT preview endpoint for checkout (`POST /events/{event_id}/tickets/vat-preview`) with buyer-specific VAT calculation and VIES caching (Redis, 30-minute TTL)

### Fixed
- Unbanning a user now clears the stale `BANNED` `OrganizationMember` row that the ban created.
- Follower notifications (`NEW_EVENT_FROM_FOLLOWED_ORG`) no longer sent for non-public events
```

Rules:

- Lead with the *what*, not the *how*. ("Discord admin-ping channel" not "Added `discord_admin_webhook_url` setting and `send_discord_admin_ping` function").
- Backticks for code symbols (model names, field names, env vars, endpoints).
- Bold prefix `**Name**:` only for headline features that warrant top-billing.
- One sentence per bullet; sub-bullets for multi-part features.
- No author/PR-number/issue-number references — those live in commit history and PRs.

### 6. Insert into the file

- `[Unreleased]` always stays at the top, even if empty (with no subsections), so future entries have a home.
- New released sections go directly under `[Unreleased]`, in descending version order.
- When promoting `[Unreleased]` to a version: copy the content under a new `## [X.Y.Z] - <tag-date>` heading and leave `[Unreleased]` empty.
- Preserve any existing `[Unreleased]` entries — they may already document work in the new version.

### 7. Verify

```bash
# Visual diff
git diff CHANGELOG.md

# Sanity check: every tag from vA..latest has a section
git tag --sort=-v:refname | head -20
grep -E '^## \[' CHANGELOG.md
```

Do not run `make release` or push tags — the skill only edits `CHANGELOG.md`. The user runs `make release` themselves.

## Anti-patterns

- ❌ Dumping every commit subject as a bullet. Filter, group, rewrite.
- ❌ Using the date of editing as the section date — use the **tag's commit date**.
- ❌ Trusting `feat:` / `fix:` prefixes without checking the diff.
- ❌ Mentioning internal refactors, test additions, type fixes, or CI changes unless they affect users/operators.
- ❌ Inventing impact you can't confirm from the diff. If unsure, ask the user or omit.
- ❌ Overwriting existing `[Unreleased]` content when promoting it — merge, don't replace.
- ❌ Editing `pyproject.toml` or creating tags. Out of scope for this skill.
