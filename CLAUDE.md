# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

This project uses a comprehensive Makefile for development tasks:

### Primary Development Commands
- `make setup` - Complete one-time setup: creates venv, installs dependencies, sets up Docker services, and starts the server
- `make run` - Start Django development server (generates test JWTs first)
- `make jwt EMAIL=user@example.com` - Get JWT access and refresh tokens for a specific user
- `make check` - Run all code quality checks (format, lint, mypy, migration-check, i18n-check, file-length)
- `make test` - Run pytest test suite in parallel with coverage reporting. On failure, saves the failures section to `.tests.output`
- `make test-linear` - Run pytest test suite sequentially (single process)
- `make test-failed` - Re-run only failed tests

### Code Quality & Formatting
- `make format` - Auto-format code with ruff
- `make lint` - Run linting with ruff (auto-fixes issues)
- `make mypy` - Run strict type checking with mypy
- `make i18n-check` - Verify translation files are compiled and up-to-date

### Internationalization (i18n)
- `make makemessages` - Extract translatable strings and update .po files
- `make compilemessages` - Compile .po files into .mo binaries (commit after running!)
- `make i18n-check` - Verify .mo files are up-to-date with .po files

### Database Management
- `make migrations` - Create new Django migrations
- `make migrate` - Apply pending migrations
- `make bootstrap` - Initialize database with base data
- `make seed` - Populate database with test data
- `make nuke-db` - **DESTRUCTIVE**: Reset database and regenerate migration files (preserves special data migrations)
- `make restart` - **DESTRUCTIVE**: Deletes all migration files, regenerates them, restarts Docker, and bootstraps from scratch

### Background Services
- `make run-celery` - Start Celery worker for background tasks
- `make run-celery-beat` - Start Celery beat scheduler
- `make run-flower` - Start Flower (Celery monitoring UI)

### Testing Variants
- `make test-linear` - Run tests sequentially (single process, useful for debugging)

## Project Architecture

Revel is a Django-based event management platform with the following structure:

### Core Applications
- **accounts/** - User authentication, registration, JWT handling, GDPR compliance
- **events/** - Core event management, organizations, tickets, memberships, invitations
- **questionnaires/** - Dynamic questionnaire system with LLM-powered evaluation
- **notifications/** - Multi-channel notification system (in-app, email, Telegram) with user preferences and digest support
- **wallet/** - Apple Wallet pass generation for event tickets (.pkpass files)
- **geo/** - Geolocation features, city data, IP-based location detection
- **telegram/** - Telegram bot integration with FSM-based conversation flows
- **api/** - Global API configuration, exception handlers, rate limiting
- **common/** - Shared utilities, authentication, base models, admin customizations

### Key Architectural Patterns

#### Service Layer Pattern
Business logic is encapsulated in service modules:
- `events/service/` - Event management, organization services
- `accounts/service/` - User management, authentication services
- `questionnaires/service/` - Questionnaire evaluation logic
- `notifications/service/` - Notification dispatching, eligibility, digests, reminders

#### Controller Pattern (Django Ninja)
API endpoints are organized in controller classes:
- Controllers inherit from `UserAwareController` for common user/auth functionality
- Use `@api_controller` decorator with consistent tagging
- Implement pagination with `PageNumberPaginationExtra`
- Support search with `@searching` decorator

#### Permission System
- Custom permission classes in `events/controllers/permissions.py`
- Organization-based access control with roles (Owner, Staff, Member)
- Event-level permissions with eligibility checking
- JWT-based authentication with optional anonymous access

### Technology Stack
- **Backend**: Django 5+ with Django Ninja for API
- **Database**: PostgreSQL with PostGIS for geo features
- **Async Tasks**: Celery with Redis
- **Authentication**: JWT with custom user model
- **File Storage**: Local filesystem in development; configurable for production
- **Containerization**: Docker Compose for development services

### Testing Framework
- **pytest** with Django integration
- Test data generation via ORM and shared fixtures in `conftest.py`
- Coverage reporting with HTML output
- Celery task testing with pytest-celery

### Code Quality Standards
- **Formatting**: ruff (replaces black)
- **Linting**: ruff with Django-specific rules
- **Type Checking**: mypy with strict settings and Django plugin
- **Docstrings**: Google-style format required
- **Coverage**: 90%+ branch coverage required in CI pipeline

### Development Environment
- Python 3.13+ required
- **UV for dependency management** (use `uv add`/`uv remove` - NEVER use pip directly)
- Docker for services (PostgreSQL, Redis, ClamAV)
- Virtual environment automatically created in `.venv/`

### Key Configuration Files
- `pyproject.toml` - Dependencies, tool configuration (ruff, mypy, pytest)
- `Makefile` - Development commands and workflows
- `compose.yaml` - Development services
- `src/revel/settings/` - Modular Django settings

### Security Features
- GDPR compliance with data export/deletion
- File malware scanning with ClamAV
- Advanced user permissions and organization roles
- Questionnaire-based access control
- Secure file handling with quarantine system

## Testing Guidelines

When writing tests:
- Create test data via ORM and shared fixtures from `conftest.py`
- Mock external services (Celery tasks, email, file uploads)
- Test both success and error cases
- Include integration tests for complex workflows
- Maintain high test coverage

## Development Notes

- Run `make check` before committing to ensure code quality
- Use type hints for all function signatures (required by mypy --strict), including in tests and fixtures.
- Follow Google-style docstrings for public APIs
- Controllers should inherit from `UserAwareController` for consistent user handling
- Business logic belongs in service modules, not controllers or models
- **Models must never import from service modules.** Pure utility functions needed by models belong in `<app>/utils/` (e.g., `accounts/utils/email_normalization.py`). The dependency direction is: controllers → services → models/utils. Services and models may both import from utils.
- Use Django Ninja's automatic OpenAPI documentation features
- Use the context7 MCP to look up documentation
- Adhere to DRY, KISS and SOLID principles. Exceptions are allowed when they make sense.
- Do not catch exceptions for the sake of catching them. Especially in the celery tasks, it might be important to let them propagate instead of having the tasks fail silently.
- Avoid using raw dictionaries. When possible prefer TypedDict, Pydantic or Dataclasses

## Dependency Management

**IMPORTANT:** This project uses UV for dependency management. NEVER use pip directly.

### Adding Dependencies
- **Production dependencies:** `uv add <package>`
- **Development dependencies:** `uv add --dev <package>` or `uv add --group dev <package>`
- **Optional dependencies:** `uv add --optional <group> <package>`

### Removing Dependencies
- `uv remove <package>`

### Syncing Environment
- `uv sync` - Install all dependencies from lockfile
- `uv sync --dev` - Include development dependencies

### Why UV?
- Faster than pip
- Deterministic dependency resolution
- Automatic virtual environment management
- Better conflict detection

## Implementation Workflow

When working on issues or new features, follow this collaborative workflow:

### 1. Investigation & Analysis Phase
- Read the issue description thoroughly
- Explore relevant code sections using Read/Grep/Glob tools
- Identify all affected files, models, and components
- Map out dependencies and relationships

### 2. Discussion Phase (Required)
- Present findings to the user
- Propose multiple approaches with pros/cons when applicable
- Discuss implementation details, edge cases, and trade-offs
- Ask clarifying questions about:
  - Desired behavior and user experience
  - Technical preferences (library choices, patterns to follow)
  - Migration strategies and backward compatibility
  - API design decisions
  - Testing requirements
- Create a detailed implementation plan with clear phases
- Get explicit approval before proceeding to implementation

### 3. Implementation Phase
- Use TodoWrite to track progress through implementation phases
- Update todos as each phase completes
- Follow the agreed-upon plan from the discussion phase
- Raise concerns immediately if issues are discovered during implementation

### 4. Key Principles
- **No surprises**: Discuss before implementing, especially for architectural decisions
- **Be thorough**: Consider migration paths, backward compatibility, and data safety
- **Document decisions**: Explain reasoning in comments and docstrings
- **Iterative refinement**: It's better to ask questions than make assumptions

## Code Style Preferences

### Typing Imports
- **Always use**: `import typing as t` (never `from typing import ...`)
- **Access types as**: `t.Any`, `t.TYPE_CHECKING`, `t.cast()`, etc.
- **Rationale**: Consistent namespace, avoids polluting module scope, clear provenance of type constructs
- Example:
  ```python
  # Good
  import typing as t

  def process(data: t.Any) -> dict[str, t.Any]:
      if t.TYPE_CHECKING:
          from mymodule import MyType
      return t.cast(dict[str, t.Any], data)

  # Bad
  from typing import Any, TYPE_CHECKING, cast

  def process(data: Any) -> dict[str, Any]:
      ...
  ```

### Schemas (Django Ninja)
- **ModelSchema**: Only declare fields at class level when they require special handling (e.g., enum conversion to string)
- **Omit unnecessary fields**: Don't include `created_at`/`updated_at` in response schemas unless specifically needed
- **DRY**: Let ModelSchema infer field types from the model definition
- **Enum fields**: Always use the model's enum class directly (e.g., `Model.MyEnum`), never bare `str` or `Literal[...]`. This ensures the OpenAPI spec documents valid values and keeps a single source of truth.
- **Datetime fields**: Always use `pydantic.AwareDatetime` for datetime fields in schemas, never `datetime.datetime`. This ensures timezone-aware serialization in the OpenAPI spec and consistent API contracts.
- Example:
  ```python
  # Good - enum referenced from the model
  class UserSchema(ModelSchema):
      restriction_type: DietaryRestriction.RestrictionType
      class Meta:
          model = User
          fields = ["id", "name", "email"]

  # Good - plain Schema also uses model enum
  class BannerSchema(Schema):
      severity: SiteSettings.BannerSeverity

  # Good - AwareDatetime for timezone-aware fields
  from pydantic import AwareDatetime
  class EventSchema(Schema):
      starts_at: AwareDatetime
      ends_at: AwareDatetime | None = None

  # Bad - datetime.datetime loses timezone enforcement
  class EventSchema(Schema):
      starts_at: datetime.datetime

  # Bad - bare str loses enum contract
  class BannerSchema(Schema):
      severity: str

  # Bad - Literal duplicates the enum values
  class BannerSchema(Schema):
      severity: Literal["info", "warning", "error"]

  # Bad - redundant declarations
  class UserSchema(ModelSchema):
      id: UUID4
      name: str
      email: str
      created_at: datetime.datetime  # Not needed for API responses
      class Meta:
          model = User
          fields = ["id", "name", "email", "created_at"]
  ```

### Controllers (Django Ninja Extra)
- **Authentication & throttling**: Define at class level when all endpoints share the same configuration
- **Per-endpoint overrides**: Use `throttle=WriteThrottle()` only for POST/PATCH/DELETE that modify data
- **Default throttle**: `UserDefaultThrottle` for GET endpoints, `WriteThrottle` for mutations
- **Flat functions**: Avoid nested conditionals. Use early returns and `model_dump(exclude_unset=True)` for updates
- **No try-except in views**: Use `get_object_or_404()` or `self.get_object_or_exception()` for lookups
- Example:
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

  # Bad - repetitive auth/throttle, nested ifs
  @api_controller("/api", throttle=AuthThrottle())
  class MyController(ControllerBase):
      @route.post("/items", auth=I18nJWTAuth(), throttle=WriteThrottle())
      def create_item(self, payload: Schema) -> Item:
          return Item.objects.create(**payload.model_dump())

      @route.patch("/items/{id}", auth=I18nJWTAuth(), throttle=WriteThrottle())
      def update_item(self, id: UUID, payload: UpdateSchema) -> Item:
          try:
              item = Item.objects.get(id=id, user=self.user())
          except Item.DoesNotExist:
              raise Http404

          if payload.field1 is not None:
              item.field1 = payload.field1
          if payload.field2 is not None:
              item.field2 = payload.field2
          item.save()
          return item
  ```

### Exception Handling (per-app handlers)

Ninja Extra has **no per-controller exception hook**, so we keep controllers free of
try/except by mapping exception types to HTTP status codes once per app. Each app
**self-registers** its handlers on the global `NinjaExtraAPI` from its `AppConfig.ready`.

- **Reusable building blocks** live in `common/exception_handlers.py`:
  - `format_validation_error(exc)` — flatten a Django `ValidationError` (or any exception) to one line
  - `make_simple_handler(status)` — render `str(exc)` as `{"detail": ...}` (use when the exception is raised *with* a message or `ValidationError` content)
  - `make_static_handler(status, message)` — render a fixed, translatable message (use for bare-raised exceptions where `str(exc)` is empty)
  - `register_handlers(api, HANDLERS)` — install a mapping on the API
- **Each app** declares a `HANDLERS: dict[type[Exception], ExceptionHandler]` table in `<app>/exception_handlers.py` and a `register()` that imports the global `api` lazily, then calls `register_handlers(api, HANDLERS)` from `AppConfig.ready`.
- **`api/api.py` stays app-agnostic** — only truly global handlers (`Exception → 500`, `ValidationError → 400`) live there. App-specific handlers take precedence by MRO (most specific registered class wins), so they run before the generic `ValidationError` fallback.
- **New app with custom exceptions?** Add `<app>/exception_handlers.py` + wire `register()` into `AppConfig.ready` (see `polls`, `events`, `questionnaires` for reference). Do **not** add per-app handlers to `api/api.py`.

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

### Race Condition Protection
- **Use helper function**: For create operations with uniqueness constraints, use `get_or_create_with_race_protection()` from `common.utils`
- **No manual try-except**: Avoid wrapping IntegrityError manually in views
- Example:
  ```python
  # Good
  from common.utils import get_or_create_with_race_protection

  food_item, created = get_or_create_with_race_protection(
      FoodItem,
      Q(name__iexact=name),
      {"name": name}
  )

  # Bad - manual handling
  try:
      food_item = FoodItem.objects.create(name=name)
  except IntegrityError:
      food_item = FoodItem.objects.get(name__iexact=name)
  ```

### Celery Dispatch & Transactions (`transaction.on_commit`)

`settings.base` sets `DATABASES["default"]["ATOMIC_REQUESTS"] = True`, so **every HTTP
request runs inside one DB transaction** that commits only when the view returns
successfully. `.delay()` pushes the task to the broker **immediately**, regardless of
that transaction. If a worker picks the task up before the request commits, any
`Model.objects.get(pk=…)` for a row created in the same request raises `DoesNotExist`
(intermittent, load-dependent, hard to reproduce).

- **Default rule**: when dispatching a Celery task from anything reachable by an HTTP
  request (a controller, a service called by a controller, or a `post_save`/`post_delete`
  signal handler firing during a request), defer the dispatch:
  ```python
  from django.db import transaction
  transaction.on_commit(lambda: my_task.delay(arg))
  ```
  If the transaction rolls back, the dispatch is correctly skipped.
- **Don't wrap task-to-task chains**: code that only runs *inside* a Celery worker (a
  `@shared_task` calling another task) is not under `ATOMIC_REQUESTS`; bare `.delay()` is
  fine there unless the task opens its own `@transaction.atomic` block first.
- **Loop variables**: never `transaction.on_commit(lambda: task.delay(x))` inside a `for x
  in …` loop — the callback runs after the loop ends and captures the *last* `x`. Build
  the values first and dispatch them in a single `on_commit` closure, or bind eagerly with
  `functools.partial(task.delay, x)`.
- **Dispatch-then-`raise` (anti-enumeration) paths are the exception**: if a handler
  intentionally dispatches a task and then raises (rolling the request back) — and the task
  targets a row that *already existed* before this request — use a bare `.delay()`. An
  `on_commit` callback registered inside an atomic block that then rolls back is discarded,
  so the task would never run. See `accounts/service/account.register_user` /
  `send_verification_email_for_user(..., defer=False)`.
- **Tests**: pytest-django's default `django_db` rolls the wrapping transaction back, so
  `on_commit` callbacks never fire (and with `CELERY_TASK_ALWAYS_EAGER` the eager task
  never runs). A test asserting the dispatch/side effect must either wrap the trigger in
  the `django_capture_on_commit_callbacks(execute=True)` fixture or be marked
  `@pytest.mark.django_db(transaction=True)` with a docstring explaining why.

### Server-Side Cursors & Connection Pooling (`.iterator()`)

Production runs **PgBouncer in transaction-pooling mode**, so `DATABASES["default"]`
sets `DISABLE_SERVER_SIDE_CURSORS = True` whenever `USE_PGBOUNCER` is on (Django's
documented requirement for that mode). Keep this in mind when iterating querysets:

- **Never combine `QuerySet.iterator()` with a per-row `transaction.atomic()` commit
  inside the loop.** `.iterator()` opens a PostgreSQL **server-side cursor**; transaction
  pooling recycles the backend connection at every `COMMIT`, orphaning that cursor, so the
  next `fetchmany()`/`close()` raises `psycopg.errors.InvalidCursorName`. This is
  load-/data-dependent (only fires when the loop body runs ≥1 commit) and **cannot be
  reproduced in tests** (plain Postgres, no pooler, and psycopg3's `WITH HOLD` cursor
  survives commits on a single backend). It bit `expire_subscriptions_past_grace` and
  `close_polls_due` — see #458 and `docs/postmortems/0002-server-side-cursor-pgbouncer.md`.
- **Correct pattern for "snapshot a candidate set, then process each under its own lock":**
  materialize the IDs with `list(...)`, then loop and re-fetch each row with
  `select_for_update()` in its own `transaction.atomic()`:
  ```python
  ids = list(Model.objects.filter(...).values_list("id", flat=True))
  for pk in ids:
      with transaction.atomic():
          obj = Model.objects.select_for_update().get(pk=pk)
          ...  # re-check preconditions inside the lock, then save
  ```
  IDs are tiny, so `.iterator()` buys no real memory savings here anyway.
- **`.iterator()` is still fine** for read-only streaming with no mid-loop DB commits
  (e.g. dispatching Celery tasks per row). Note that with `DISABLE_SERVER_SIDE_CURSORS`
  it degrades to a client-side fetch-all — for genuinely large streams, batch explicitly.

### Query Optimization
- **Prefetch relationships**: Use `select_related()` for foreign keys, `prefetch_related()` for reverse/M2M
- **Avoid N+1 in schemas**: When ModelSchema includes nested relationships, ensure the queryset prefetches them
- **Example**:
  ```python
  # Controller with proper prefetching
  def list_items(self) -> QuerySet[Item]:
      return Item.objects.select_related("category").prefetch_related("tags")
  ```

### Service Layer Patterns

This project uses a **hybrid approach** to services: function-based for stateless operations, class-based for stateful workflows. This is intentional.

#### When to Use Function-Based Services
- **Single-purpose operations**: CRUD, validation, simple queries
- **Cross-entity operations**: Operations spanning multiple models without shared context
- **Utility helpers**: Formatting, calculations, lookups

```python
# Good - stateless operations as functions
def add_to_blacklist(organization: Organization, email: str, ...) -> Blacklist:
    return Blacklist.objects.create(organization=organization, email=email, ...)

def create_venue(organization: Organization, payload: VenueCreateSchema) -> Venue:
    return Venue.objects.create(organization=organization, **payload.model_dump())
```

#### When to Use Class-Based Services
- **Request-scoped workflows**: Operations that share context (user, event, organization)
- **Multi-step processes**: Checkout flows, eligibility checks, complex validations
- **Stateful computations**: When you'd pass the same 3+ arguments to multiple related functions

```python
# Good - stateful workflow as class
class BatchTicketService:
    def __init__(self, event: Event, tier: TicketTier, user: RevelUser) -> None:
        self.event = event
        self.tier = tier
        self.user = user

    def create_batch(self, items: list[TicketPurchaseItem]) -> list[Ticket] | str:
        # Validates batch size, resolves seats, delegates to payment flow
        ...
```

#### Mixed Modules Are OK
A single service module can contain both patterns when they serve different purposes:
- `batch_ticket_service.py` has `BatchTicketService` (ticket purchase workflow) and `ticket_service.py` has `check_in_ticket()` (standalone operation)
- This is intentional - don't force everything into one pattern

#### Controller Integration
```python
# Function-based - import module, call directly
from events.service import blacklist_service
entry = blacklist_service.add_to_blacklist(organization, email=email)

# Class-based - instantiate per request
from events.service.batch_ticket_service import BatchTicketService
service = BatchTicketService(event=event, tier=tier, user=user)
result = service.create_batch(items=purchase_items)
```

#### Why Not Dependency Injection?
We intentionally avoid Django Ninja Extra's DI container because:
- Our services are **request-contextual** (need user, event, etc.) - doesn't fit singleton pattern
- Explicit instantiation makes code **traceable** (grep finds all usages)
- No framework lock-in beyond Django Ninja
- KISS principle - manual instantiation is simple and clear

## Note to claude
- Do not run tests. Let the user run the tests.
- Always discuss implementation approach before writing code for non-trivial changes.