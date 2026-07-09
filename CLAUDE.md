# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

> **Layout:** _Operating Principles_ (how you work — read first) → _Development Commands_ →
> _Architecture_ → _Project Rules_ (style, schemas, controllers, services) → _Workflow_ →
> _Hard Rules_. Deep production gotchas live in [`docs/engineering-notes.md`](docs/engineering-notes.md).

---

## Operating Principles

How you work, *before* any project rule below. These bias toward caution over speed; for
trivial tasks, use judgment.

### 1. Think before coding
**Don't assume. Don't hide confusion. Surface tradeoffs.**
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop, name what's confusing, and ask.

This is the always-on, lightweight form of the [Discussion Phase](#implementation-workflow);
the full ritual still applies to non-trivial features.

### 2. The best code is the code you never wrote
**Minimum code that solves the problem. Nothing speculative.** Before writing, stop at the
first rung that satisfies the task:

1. **Does it need to exist?** → skip it (YAGNI).
2. **Does the stdlib / Django / Ninja already do it?** → use the built-in.
3. **Is it already an installed dependency?** → reuse it. Check `pyproject.toml` before
   reaching for `uv add`; a new dependency is a last resort, not a first move.
4. **Can it be one line?** → write one line.
5. **Only then** → write the minimum that works.

Boring over clever. Fewest files, shortest diff wins. Reject unrequested abstractions
(interfaces/factories for a single implementation), "flexibility" nobody asked for, and
error handling for impossible scenarios. If you wrote 200 lines and it could be 50, rewrite it.
Ask: *"Would a senior engineer call this overcomplicated?"* If yes, simplify.

**Lazy, not negligent.** Never simplify away: trust-boundary/input validation, data-loss &
error handling, security, permission/eligibility checks, or accessibility. "Lazy" means less
code — never a flimsier algorithm. When you deliberately cap a solution, leave a
`# ponytail:` comment noting the ceiling and the upgrade path.

### 3. Surgical changes
**Touch only what you must. Clean up only your own mess.**
- Don't "improve" adjacent code, comments, or formatting. Don't refactor what isn't broken.
- Match existing style, even if you'd do it differently.
- Remove imports/variables/functions that *your* change orphaned; flag pre-existing dead
  code, don't delete it.
- The test: every changed line should trace directly to the request.

### 4. Goal-driven execution
**Define success criteria. Loop until verified.** Turn tasks into verifiable goals:
- "Add validation" → write tests for invalid inputs, then make them pass.
- "Fix the bug" → write a test that reproduces it, then make it pass.
- "Refactor X" → ensure tests pass before and after.

State a brief plan for multi-step work (`step → verify: check`). Strong success criteria let
you loop independently; weak ones ("make it work") force constant clarification.
**Project caveat: you write the tests — the user runs them** (see [Hard Rules](#hard-rules)).

**These principles are working if:** fewer unnecessary changes in diffs, fewer rewrites from
overcomplication, and clarifying questions arrive *before* implementation, not after mistakes.

---

## Development Commands

This project uses a comprehensive Makefile.

### Primary
- `make setup` — One-time setup: venv, deps, Docker services, server.
- `make run` — Start Django dev server (generates test JWTs first).
- `make jwt EMAIL=user@example.com` — Get JWT access/refresh tokens for a user.
- `make check` — All quality checks (format, lint, mypy, migration-check, i18n-check, file-length, task-names).
- `make test` — Parallel pytest with coverage; on failure saves the failures section to `.tests.output`.
- `make test-linear` — Sequential tests (single process, for debugging).
- `make test-failed` — Re-run only failed tests.

### Code quality
- `make format` — ruff auto-format · `make lint` — ruff lint (auto-fix) · `make mypy` — strict type check.

### i18n
- `make makemessages` — extract/update `.po` · `make compilemessages` — compile to `.mo` (**commit after!**) · `make i18n-check` — verify `.mo` up-to-date.

### Database
- `make migrations` / `make migrate` — create / apply migrations.
- `make bootstrap` — base data · `make seed` — test data.
- `make nuke-db` — **DESTRUCTIVE**: reset DB, regenerate migrations (preserves special data migrations).
- `make restart` — **DESTRUCTIVE**: delete migrations, regenerate, restart Docker, bootstrap from scratch.

### Background services
- `make run-celery` / `make run-celery-beat` / `make run-flower` / `make run-stripe` / `make run-telegram`.

---

## Project Architecture

Revel is a Django-based event management & ticketing platform.

### Core applications
- **accounts/** — auth, registration, JWT, GDPR compliance.
- **events/** — events, organizations, tickets, memberships, invitations.
- **questionnaires/** — dynamic questionnaires with LLM-powered evaluation.
- **notifications/** — multi-channel (in-app, email, Telegram) with preferences & digests.
- **polls/** — Organization-managed polls
- **wallet/** — Apple Wallet `.pkpass` generation.
- **geo/** — geolocation, city data, IP-based location.
- **telegram/** — Telegram bot with FSM conversation flows.
- **api/** — global API config, exception handlers, rate limiting.
- **common/** — shared utilities, auth, base models, admin customizations.

### Key patterns
- **Service layer** — business logic in `<app>/service/`. Hybrid: function-based for stateless
  ops, class-based for request-scoped workflows (see [Service Layer Patterns](#service-layer-patterns)).
- **Controllers (Django Ninja Extra)** — inherit `UserAwareController`; `@api_controller` with
  consistent tags; `PageNumberPaginationExtra`; `@searching` for search.
- **Permissions** — `events/controllers/permissions.py`; org roles (Owner/Staff/Member);
  event-level eligibility; JWT auth with optional anonymous access.

### Stack
Django 5+ / Django Ninja · PostgreSQL + PostGIS · Celery + Redis · JWT (custom user model) ·
Docker Compose (Postgres, Redis, ClamAV). Python 3.14+. **UV** for deps (never pip).

### Key config files
`pyproject.toml` (deps + ruff/mypy/pytest), `Makefile`, `compose.yaml`, `src/revel/settings/`.

### Quality bar
ruff (format + lint) · mypy `--strict` + Django plugin · Google-style docstrings ·
**90%+ branch coverage** in CI · 1000-line-per-file limit (enforced by `make check`).

---

## Project Rules

Conventions specific to this codebase. The *why* is in [Operating Principles](#operating-principles);
these are the concrete forms.

### Typing imports
- **Always** `import typing as t` (never `from typing import ...`); access as `t.Any`, `t.TYPE_CHECKING`, `t.cast()`.
- Type hints on **all** signatures (mypy `--strict`), including tests and fixtures.

```python
# Good
import typing as t

def process(data: t.Any) -> dict[str, t.Any]:
    if t.TYPE_CHECKING:
        from mymodule import MyType
    return t.cast(dict[str, t.Any], data)
```

### Schemas (Django Ninja)
- **ModelSchema**: only declare fields at class level when they need special handling (e.g. enum→string).
- **Omit** `created_at`/`updated_at` from response schemas unless needed. Let ModelSchema infer types.
- **Enum fields**: use the model's enum class directly (e.g. `Model.MyEnum`) — never bare `str` or `Literal[...]`.
- **Datetime fields**: always `pydantic.AwareDatetime`, never `datetime.datetime`.
- New schemas in `events/schema/*.py` must be re-exported from `events/schema/__init__.py`
  (import **and** `__all__`) or mypy can't resolve `schema.NewSchema` in controllers.

```python
# Good — enum referenced from the model, AwareDatetime for tz-aware fields
from pydantic import AwareDatetime

class UserSchema(ModelSchema):
    restriction_type: DietaryRestriction.RestrictionType
    class Meta:
        model = User
        fields = ["id", "name", "email"]

class EventSchema(Schema):
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None

# Bad — loses the enum/timezone contract or redeclares inferred fields
class BannerSchema(Schema):
    severity: str                      # use SiteSettings.BannerSeverity
class EventSchema(Schema):
    starts_at: datetime.datetime       # use AwareDatetime
```

### Controllers (Django Ninja Extra)
- **Auth & throttling** at class level when shared; override per-endpoint only for mutations
  (`throttle=WriteThrottle()`). Default: `UserDefaultThrottle` for GET, `WriteThrottle` for writes.
- **Flat functions** — early returns, `model_dump(exclude_unset=True)` for updates.
- **No try/except in views** — use `get_object_or_404()` / `self.get_object_or_exception()`.
- **Thin controllers** — business logic (VAT checks, rollback, multi-step flows) belongs in services.

```python
# Good
@api_controller("/api", auth=I18nJWTAuth(), throttle=UserDefaultThrottle())
class MyController(ControllerBase):
    @route.post("/items", throttle=WriteThrottle())
    def create_item(self, payload: Schema) -> Item:
        return Item.objects.create(**payload.model_dump())

    @route.patch("/items/{id}", throttle=WriteThrottle())
    def update_item(self, id: UUID, payload: UpdateSchema) -> Item:
        item = get_object_or_404(Item, id=id, user=self.user())
        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return item
        for field, value in update_data.items():
            setattr(item, field, value)
        item.save(update_fields=list(update_data.keys()))
        return item
```

> **Ninja `Query`/`Body` params — use the `Annotated` idiom** (not a `# type: ignore[type-arg]`):
> put the real type in the annotation and the `Query()`/`Body()` object in the metadata. This
> passes `mypy --strict` cleanly and preserves the OpenAPI constraints.
> - `params: FilterSchema = Query(...)` → `params: t.Annotated[FilterSchema, Query(...)]` (required — drop the `=` default)
> - `month: int | None = Query(None, ge=1, le=12)` → `month: t.Annotated[int | None, Query(ge=1, le=12)] = None`
> - `payload: Schema | None = Body(None)` → `payload: t.Annotated[Schema | None, Body(None)] = None`
>
> **Optional JSON body**: a plain `Schema | None = None` raises "Cannot parse request body" when
> no JSON is sent — keep `Body(None)` in the metadata (`t.Annotated[Schema | None, Body(None)] = None`).
> Tests must pass `content_type="application/json"` even with an empty body. When a whole-schema
> `Query(...)` param becomes required, make sure it doesn't sit after a defaulted param (Python
> `SyntaxError`); reorder so it comes first.

### Exception handling (per-app handlers)
Ninja Extra has **no per-controller exception hook**, so we keep controllers free of try/except
by mapping exception types to HTTP status codes once per app. Each app **self-registers** its
handlers on the global `NinjaExtraAPI` from its `AppConfig.ready`.

- **Reusable building blocks** in `common/exception_handlers.py`:
  - `format_validation_error(exc)` — flatten a Django `ValidationError` to one line.
  - `make_simple_handler(status)` — render `str(exc)` as `{"detail": ...}` (exception raised *with* a message).
  - `make_static_handler(status, message)` — fixed translatable message (bare-raised, empty `str(exc)`).
  - `register_handlers(api, HANDLERS)` — install a mapping on the API.
- **Each app** declares `HANDLERS: dict[type[Exception], ExceptionHandler]` in
  `<app>/exception_handlers.py` + a `register()` that lazily imports the global `api`.
- **`api/api.py` stays app-agnostic** — only global handlers (`Exception → 500`,
  `ValidationError → 400`). App handlers win by MRO (most specific class registered).
- **New app with custom exceptions?** Add `<app>/exception_handlers.py` and wire `register()`
  into `AppConfig.ready` (see `polls`, `events`, `questionnaires`). Do **not** add per-app
  handlers to `api/api.py`.

```python
# <app>/exception_handlers.py
from common.exception_handlers import ExceptionHandler, make_simple_handler, register_handlers
from <app>.exceptions import FooNotOpenError

HANDLERS: dict[type[Exception], ExceptionHandler] = {
    FooNotOpenError: make_simple_handler(423),
}

def register() -> None:
    from api.api import api
    register_handlers(api, HANDLERS)

# <app>/apps.py
class FooConfig(AppConfig):
    def ready(self) -> None:
        from <app>.exception_handlers import register as register_exception_handlers
        register_exception_handlers()
```

### Race condition protection
For create operations with uniqueness constraints, use `get_or_create_with_race_protection()`
from `common.utils` — don't wrap `IntegrityError` manually.

```python
from common.utils import get_or_create_with_race_protection

food_item, created = get_or_create_with_race_protection(
    FoodItem, Q(name__iexact=name), {"name": name}
)
```

### Service Layer Patterns
Hybrid by design: **function-based** for stateless single-purpose / cross-entity ops;
**class-based** for request-scoped, multi-step, stateful workflows (3+ shared args). A module
may contain both. We intentionally avoid Ninja Extra's DI container — services are
request-contextual, explicit instantiation is grep-able, and it keeps things simple (KISS).

```python
# Function-based — stateless
def create_venue(organization: Organization, payload: VenueCreateSchema) -> Venue:
    return Venue.objects.create(organization=organization, **payload.model_dump())

# Class-based — request-scoped workflow
class BatchTicketService:
    def __init__(self, event: Event, tier: TicketTier, user: RevelUser) -> None:
        self.event, self.tier, self.user = event, tier, user

    def create_batch(self, items: list[TicketPurchaseItem]) -> list[Ticket] | str:
        ...  # validate batch, resolve seats, delegate to payment flow

# Controller integration
from events.service import blacklist_service
entry = blacklist_service.add_to_blacklist(organization, email=email)

from events.service.batch_ticket_service import BatchTicketService
result = BatchTicketService(event=event, tier=tier, user=user).create_batch(items=purchase_items)
```

### Query optimization
- `select_related()` for FKs, `prefetch_related()` for reverse/M2M.
- When a ModelSchema nests relationships, prefetch them in the controller queryset (avoid N+1).

```python
def list_items(self) -> QuerySet[Item]:
    return Item.objects.select_related("category").prefetch_related("tags")
```

### Other conventions
- **Dependency direction**: controllers → services → models/utils. **Models must never import
  from services.** Pure model helpers go in `<app>/utils/` (e.g. `accounts/utils/email_normalization.py`).
- **Don't catch exceptions for the sake of it** — especially in Celery tasks, let them
  propagate rather than failing silently.
- **Avoid raw dicts** — prefer `TypedDict`, Pydantic, or dataclasses.
- **Version bumps**: never hand-edit `version` in `pyproject.toml` — use `uv version --bump <part>`
  so `uv.lock` stays in sync.
- **Celery beat cadence**: no per-minute jobs — default 5 min, 15 min for heavier sweeps,
  sub-5-min only if genuinely cheap.
- **Celery task names — always pin `name=`**: every `@shared_task` **must** pass an explicit
  `name=` (e.g. `@shared_task(name="events.cleanup_expired_payments")`). A bare `@shared_task`
  takes Celery's *default* name derived from the module path (`<module>.<func>`), so moving or
  renaming the module silently changes the registered name — which breaks `django_celery_beat`
  `PeriodicTask` rows (they reference tasks by name string) and strands in-flight messages across
  a deploy. Enforced by `make task-names` (in `make check` **and** CI). When moving an existing
  task, keep its current name verbatim. To catch beat rows pointing at a now-missing task (e.g.
  manually-created ones), run `python manage.py check_orphaned_beat_tasks`.
- Use Django Ninja's automatic OpenAPI docs. Use the **context7 MCP** to look up library docs.

> ⚠️ **Deep production gotchas** — Celery `transaction.on_commit` / `ATOMIC_REQUESTS`, and
> PgBouncer server-side cursors with `.iterator()` — live in
> [`docs/engineering-notes.md`](docs/engineering-notes.md). **Read them before dispatching
> Celery tasks from a request or iterating a large queryset.**

---

## Dependency Management

**UV only — never pip.**
- Add: `uv add <package>` · dev: `uv add --dev <package>` · optional: `uv add --optional <group> <package>`.
- Remove: `uv remove <package>` · Sync: `uv sync` (`--dev` to include dev deps).
- Why: faster, deterministic resolution, managed venv, better conflict detection.

---

## Testing

- Create test data via ORM and shared fixtures from `conftest.py`.
- Mock external services (Celery, email, file uploads). Test success **and** error cases.
- Include integration tests for complex workflows; maintain 90%+ coverage.
- **`on_commit` / eager-task caveat**: pytest-django rolls the wrapping transaction back, so
  `on_commit` callbacks never fire. Asserting a dispatch/side effect requires
  `django_capture_on_commit_callbacks(execute=True)` or `@pytest.mark.django_db(transaction=True)`
  with a docstring explaining why. (Details in [engineering-notes](docs/engineering-notes.md).)

---

## Implementation Workflow

For issues and non-trivial features — the full form of Operating Principle #1:

1. **Investigate** — read the issue, explore with Read/Grep/Glob, map affected files & dependencies.
2. **Discuss (required)** — present findings; propose approaches with pros/cons; raise edge cases,
   migration/backward-compat, API design, and testing needs; agree a phased plan; **get explicit
   approval before implementing**.
3. **Implement** — track phases with TodoWrite; follow the agreed plan; raise concerns the moment
   they surface.
4. **Principles** — no surprises, be thorough about migrations/data safety, document decisions,
   prefer questions over assumptions.

---

## Hard Rules

Non-negotiable, project-specific:
- **Do not run the full test suit. Run the tests that cover the code you modified..**
- **Never commit to `main`.** Feature branches: `feature/issue-number-description` /
  `fix/issue-number-description`. Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`).
- **Always ask before committing** — show `git status` and draft the message first.
- **Always discuss the approach before writing code for non-trivial changes** (Principle #1).
- **UV, never pip.** **Models never import services.**
- **Avoid circular dependencies.**
