# Gauntlet profile: revel-backend

## About this codebase
Revel is an event management and ticketing platform. The backend is a Django 5.2 REST API (Python 3.13+, Django Ninja Extra, PostgreSQL/PostGIS, Celery with Redis, `uv` for dependencies) under `src/`, one Django app per bounded area (`events`, `accounts`, `oauth`, `integrations`, `notifications`, `questionnaires`, `polls`, `telegram`, `wallet`, `geo`, `moderation`, plus `common`, `api` and the `revel` settings package). It is multi-tenant: organizations own events, series, venues, members and (optionally) a white-label custom domain; users hold Owner, Staff (with fine-grained permissions) or Member roles per organization, and event visibility and eligibility are enforced per event. Money moves through Stripe Connect: ticket checkout (including guest checkout and seated events), series passes, membership subscriptions, refunds, a platform fee, VAT and EU e-invoicing, plus attendee invoices and credit notes issued on behalf of organizations. Authentication is JWT (refresh rotation) with OIDC login (relying party), 2FA, and an OAuth/OIDC provider for third-party apps. The API is consumed by a SvelteKit frontend whose client is generated from this repo's OpenAPI schema.

## Gate
1. `make format`, then `make check` (ruff format and lint, mypy `--strict`, migration check, i18n check, i18n literals, file length, task names).
2. Targeted tests only: `uv run pytest <test paths for the touched modules> -q`. Never run the full suite.
3. If error responses changed: `uv run pytest src/api/tests/test_error_response_contracts.py`.
4. If migrations changed: one migration per app per PR; squash by deleting the branch's schema migrations and regenerating them with `makemigrations`; never hand-edit a schema migration (data migrations are hand-written). NOT NULL columns need `db_default` because rolling deploys run the migration while old code still inserts.
5. If translatable strings changed: run `make makemessages` once at the end, translate fresh, leave zero fuzzy entries, run `make compilemessages`, commit the `.po` files only (never `.mo`).
6. If the API surface changed: the frontend must run `pnpm generate:api`; say so in the PR.

Parallel test runs: give each concurrent run its own database, e.g. `DB_NAME=revel_<tag> uv run pytest ...`. Two runs against the same test database cause phantom setup errors. Run `src/events/tests/test_controllers/test_organization_admin/` in its own pytest invocation: mixing it with individual test files makes its fixtures vanish.

## Docs locations
- CHANGELOG: `CHANGELOG.md`, section `[Unreleased]` (Keep a Changelog; user-facing impact, not implementation detail).
- Docs: `docs/` (mkdocs): `docs/architecture/` per feature, `docs/guides/`, `docs/runbooks/`, `docs/compliance/`, `docs/contributing/`.
- Decisions: `docs/adr/` (index in `docs/adr/index.md`).
- Other: `docs/engineering-notes.md` (production-only gotchas: `on_commit` dispatch under `ATOMIC_REQUESTS`, `.iterator()` behind PgBouncer, checkout reserve/session split, lock ordering, refund paths, migration squashing, token worlds), `SECURITY.md`, docstrings (Google style).

## House rules reviewers should check
See `CLAUDE.md` (and `docs/contributing/code-style.md`). The ones that most often slip:
- Controllers are thin and have no try/except; business logic lives in services; dependency direction is controllers, services, models/utils. Models never import services; `common` never imports apps (use `AppConfig.ready` registration hooks).
- `import typing as t`; type hints on every signature; `pydantic.AwareDatetime` for datetimes; enum fields reference the model enum class; `t.Annotated[T, Query(...)]` for Ninja `Query`/`Body`.
- New `events/schema/*.py` schemas are re-exported from `events/schema/__init__.py` (import and `__all__`).
- Every `@shared_task` pins `name=`; Celery dispatch from request paths uses `transaction.on_commit`; no per-minute beat jobs.
- User-facing error messages are wrapped in `_()` (translate first, then `.format`); no f-strings in translated messages.
- Attendance means an ACTIVE or CHECKED_IN ticket (`Ticket.objects.attending()` / `Ticket.ATTENDING_STATUSES`); never re-derive it from `status != CANCELLED`.
- Per-app exception handlers register via `AppConfig.ready`; `api/api.py` stays app-agnostic.
- No unrequested abstractions; leave a `# ponytail:` comment where a solution is deliberately capped.

## Security triggers
Run the security lane when a diff touches any of:
- Paths: `src/accounts/**`, `src/oauth/**`, `src/wallet/**`, `src/common/authentication.py`, `src/common/auth_base.py`, `src/common/throttling.py`, `src/common/signing.py`, `src/common/sanitizers.py`, `src/common/service/upload_service.py`, `src/revel/settings/**`, `**/permissions.py`, `**/controllers/**` that touch authentication or role checks, `src/events/service/stripe*`, `src/events/service/batch_ticket_service/**`, `src/events/service/custom_domain_service.py`, `src/events/controllers/tls_ask.py`, `src/events/compliance/**`, `src/events/utils/hostname.py`, `src/events/utils/visibility*.py`, `src/events/utils/__init__.py`.
- Keywords in a path or the diff: `token`, `oidc`, `auth`, `permission`, `payment`, `stripe`, `refund`, `invoice`, `webhook`, `checkout`, `guest`, `custom_domain`, `tenant`, `upload`, `file`, `signing`, `secret`.
- Dependencies, CI and packaging: `pyproject.toml`, `uv.lock`, `.github/workflows/**`, `Dockerfile*`, `docker-compose*.yml`.
- Anything that pairs with the reverse proxy config in `../infra/Caddyfile` (host handling, client IP, forwarded headers, TLS ask).

## Sibling repos
Read-only context for hunters (do not review or edit):
- `../infra`: `Caddyfile` (host routing, forwarded headers, TLS ask, SSR streaming) and the compose files.
- `../revel-frontend`: the SvelteKit client; use it to check how an endpoint's response is consumed (what the UI relies on) before calling a response field a leak.

## External reviewer and merge policy
- External reviewer: CodeRabbit (free tier, about 2 reviews per rate-limit window; after the window, comment `@coderabbitai full review`; it does not review closed PRs).
- Skip external review for: internal or admin-only PRs (they go through the self-review lanes only).
- Merge: never merge and never enable auto-merge without explicit approval for that specific PR. Squash only. Keep history linear: if a PR is behind `main`, rebase and force-push, never merge `main` into the branch. Resolve CodeRabbit threads before merging.

## Agent memory
Code-level suppressions and the findings ledger live outside git, in the main checkout: `.claude/agent-memory/<agent>/MEMORY.md` (gitignored; worktrees do not have it, so pass the absolute path of the main checkout's copy to agents). Agents never write it; the orchestrating skill does, only with verifier-confirmed facts after maintainer approval. Policy-level by-design rules are in `.claude/review/by-design.md`.

## Debt metrics
Deterministic, run from the repo root. Use `git ls-files`/`git grep` so untracked and ignored files never count.
- Python files over 900 lines under `src` (excluding migrations, tests; the hard cap is 1000 and `make check` enforces it):
  `git ls-files 'src/*.py' | grep -v -e /migrations/ -e /tests/ | xargs wc -l | awk '$2!="total" && $1>900'`
- `type: ignore` count in non-test source:
  `git grep -c 'type: ignore' -- 'src/*.py' ':!src/**/tests/**' | awk -F: '{s+=$2} END{print s}'`
- `noqa` count in non-test source:
  `git grep -c 'noqa' -- 'src/*.py' ':!src/**/tests/**' | awk -F: '{s+=$2} END{print s}'`
- Migrations per app (total, then added since the last release tag; the rule is one schema migration per app per PR):
  `git ls-files 'src/*/migrations/0*.py' | cut -d/ -f2 | sort | uniq -c | sort -rn`
  `git diff --name-only --diff-filter=A "$(git describe --tags --abbrev=0)"..HEAD -- 'src/*/migrations/0*.py'`
- `# ponytail:` markers (deliberately capped solutions, each should name its ceiling and upgrade path):
  `git grep -n 'ponytail:' -- src`
- Bare `@shared_task` without `name=`: `make task-names`.
- Unused imports and variables: `uv run ruff check --select F401,F841 src`. Dead symbols: have the debt hunter prove zero references with `git grep` before reporting.
- Test weight (for the tests specialist): `git ls-files 'src/**/tests/**/*.py' | xargs wc -l | sort -rn | head`.
