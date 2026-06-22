---
name: whats-new
description: Draft a "What's New" community update for the Revel Discord covering changes across backend, frontend, and infra over a time window. Use when the user runs /whats-new or asks for a community changelog/release-roundup post for organizers (and the programmers in the room).
allowed-tools: Bash(git:*), Bash(gh:*), Bash(date:*), Bash(grep:*), Bash(ls:*), Bash(test:*), Read, Edit, Write, Glob, Grep
---

# What's New

Produces a Discord-ready "What's New" post for the Revel community and archives it on the
published docs site. Audience: **mostly organizers, but programmers too** — every item leads
with the organizer-facing benefit and carries a light technical detail (a flag, an endpoint, a
behavior change) for the developers reading along.

Three repos, all **public**, all siblings of the cwd (`revel-backend`):

| Repo | Path | Primary source |
|------|------|----------------|
| Backend | `.` (cwd) | `CHANGELOG.md` (rich, dated, user-facing prose) |
| Frontend | `../revel-frontend` | `CHANGELOG.md` **if present**, else git/PR digging |
| Infra | `../infra` | git/PR digging (no changelog) |

> The frontend is adopting the backend's changelog process. **Always check for
> `../revel-frontend/CHANGELOG.md` first** and use it changelog-first when it exists; only fall
> back to git/PR digging for repos that have no changelog yet.

## 1. Parse the window

The argument is `number + w|m` (e.g. `1w`, `2m`, `6w`). **Default `1m`** if empty/unrecognized.

Compute the cutoff date (this is darwin/BSD `date`; for GNU use `date -d "-2 months" +%F`):

```bash
# N and UNIT parsed from the arg; UNIT is w (weeks) or m (months)
date -v-${N}${UNIT} +%F        # e.g. date -v-1m +%F  → cutoff YYYY-MM-DD
date +%F                       # today, for the edition header
```

Keep the human label for the header: "Last month" (1m), "Last 2 weeks" (2w), etc.

## 2. Gather changes (changelog-first, dig only gaps)

**Backend** — primary, already curated prose:

```bash
grep -nE '^## \[' CHANGELOG.md          # locate version sections + dates
```

Read every `## [X.Y.Z] - DATE` section with `DATE >= cutoff`, plus `[Unreleased]`. These bullets
are already user-facing — reuse their substance. Open a PR (`gh pr view <n>`) **only** when an
entry is too terse to translate for a community audience.

**Frontend** — changelog-first if present, else git/PR:

```bash
test -f ../revel-frontend/CHANGELOG.md && echo "use changelog" || echo "dig git/PRs"
# changelog path: same as backend, against ../revel-frontend/CHANGELOG.md
# fallback path:
git -C ../revel-frontend log --since="<cutoff>" --first-parent --merges --pretty='%h %s'
gh -R letsrevel/revel-frontend pr list --state merged --search "merged:>=<cutoff>" \
   --limit 50 --json number,title,body,mergedAt
```

**Infra** — git/PR only:

```bash
git -C ../infra log --since="<cutoff>" --first-parent --merges --pretty='%h %s'
gh -R letsrevel/infra pr list --state merged --search "merged:>=<cutoff>" \
   --limit 50 --json number,title,body,mergedAt
```

When a PR title is opaque, read its body for the user-facing description. **Drop the noise**:
pure CI/lint/test/dependency-bump/refactor commits unless they change observable behavior.

## 3. Compose — grouped by area

Group items into emoji-headed areas chosen from what's actually in the window. Suggested palette
(use only the ones that apply; invent others as needed):

- `## 🎟️ Events & Tickets`
- `## 💳 Payments, VAT & Reporting`
- `## 🔔 Notifications & Comms`
- `## 📋 Questionnaires & Polls`
- `## 🌍 Self-hosting & Infrastructure`
- `## 🛠️ Under the Hood` (perf, security, developer-facing)

Per bullet: **organizer benefit first, light tech detail inline.** Example:

> - Mark an event as **open-ended** when there's no fixed finish — the page shows "Ongoing"
>   instead of an end time (`is_open_ended` flag, propagates to recurring series).

Rules:
- **Discord markdown only**: `##`/`###` headers, `**bold**`, `-` bullets, `` `code` ``. No tables,
  no collapsibles.
- Confident and community-friendly, concise, no marketing fluff.
- Don't surface internal refactors, secrets, or per-PR links in the body.

## 4. The changelog footer (REQUIRED on every edition page)

Every edition page **must** end with a footer linking the changelog(s) — include a repo only if it
has a changelog. mkdocs has no auto-link extension, so use **markdown links**:

```markdown
**📋 Full changelogs:** [Backend](https://github.com/letsrevel/revel-backend/blob/main/CHANGELOG.md) · [Frontend](https://github.com/letsrevel/revel-frontend/blob/main/CHANGELOG.md)
```

Only list Frontend once `../revel-frontend/CHANGELOG.md` exists; add Infra if/when it gets one.

## 5. Deliver

The published docs page is the canonical artifact; the Discord message is a short teaser that
links to it.

**a. Write a new edition page** (one file per run):
`docs/whats-new/<today>-last-<slug>.md`, where `<slug>` is the window label kebab-cased —
`1m`→`last-month`, `2m`→`last-2-months`, `1w`→`last-week`, `2w`→`last-2-weeks`. Example:
`docs/whats-new/2026-06-22-last-month.md`. Structure: `# What's New — <date range>` H1, the
area sections (`##`), then the §4 changelog footer.

**b. Index it** — prepend a bullet to `docs/whats-new/index.md` right after the
`<!-- EDITIONS BELOW ... -->` marker, newest first:

```markdown
- [<date> — Last <window>](<filename-without-.md>.md) — <one-line teaser of the headline items>
```

(Dated pages are kept out of the nav via `not_in_nav: whats-new/20*.md` in `mkdocs.yml`; the index
is their table of contents. New files are reachable by URL and from the index — no nav edit needed.)

**c. Print the Discord message in chat**, inside a fenced block so it's copy-pasteable — a SHORT
teaser, not the full post: a punchy intro line, **4–6 headline bullets** (the biggest items only),
and a link to the published edition page:

```
📖 Full update → https://docs.letsrevel.io/whats-new/<slug>/
```

(The `<slug>` is the filename without `.md`. The URL goes live when docs deploy — that's expected;
say so.) Keep it comfortably under Discord's 2000-char limit.

**d. Do not commit** (project rule: always ask first). Report the new file, the index update, and
print the Discord teaser, then offer to commit.

## Notes

- The docs publish to https://docs.letsrevel.io under the "What's New" nav tab; each edition lives
  at `https://docs.letsrevel.io/whats-new/<slug>/`.
- The edition page carries the full detail; the Discord teaser only summarizes and links to it.
