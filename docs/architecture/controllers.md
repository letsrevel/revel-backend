# Controllers

Revel uses [Django Ninja Extra](https://django-ninja-extra.readthedocs.io/) controllers to organize API endpoints into cohesive, class-based units. Controllers handle request parsing, authentication, throttling, and response serialization, but **not** business logic.

## Core Patterns

### Controller Structure

All controllers follow the same blueprint:

- Inherit from `UserAwareController` for common user/auth functionality
- Use `@api_controller` decorator with consistent tagging
- Define authentication and throttling at **class level** when shared
- Override throttling **per-endpoint** only for mutations

```python
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

### Throttling Rules

| HTTP Method | Default Throttle | Override Needed? |
|---|---|---|
| `GET` | `UserDefaultThrottle` (class-level) | No |
| `POST` | `WriteThrottle` | Yes (per-endpoint) |
| `PATCH` | `WriteThrottle` | Yes (per-endpoint) |
| `DELETE` | `WriteThrottle` | Yes (per-endpoint) |

## Good vs Bad Patterns

=== "Good"

    ```python
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

    - Auth/throttle at class level (DRY)
    - Flat control flow with early return
    - `model_dump(exclude_unset=True)` for partial updates
    - `get_object_or_404` for lookups

=== "Bad"

    ```python
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

    - Repetitive `auth=` on every endpoint
    - Manual try/except instead of `get_object_or_404`
    - Nested conditionals for each field
    - `save()` without `update_fields` (updates all columns)

!!! note "Thin controllers"
    Business logic (VAT checks, rollback, multi-step flows) belongs in a [service](service-layer.md), not in the controller method.

!!! danger "No try-except in views"
    Never wrap ORM lookups in try/except blocks. Use `get_object_or_404()` or `self.get_object_or_exception()` instead. Let Django Ninja's exception handlers do their job.

## Exception Handling

Controllers have no try/except, and Ninja Extra has no per-controller exception hook, so exception types are mapped to HTTP responses once per app. Each app **self-registers** its handlers on the global `NinjaExtraAPI` from `AppConfig.ready`.

The reusable building blocks live in `common/exception_handlers.py`:

| Helper | Use |
|---|---|
| `make_simple_handler(status)` | Renders `str(exc)` as `{"detail": ...}`; for exceptions raised **with** a message |
| `make_static_handler(status, message)` | Renders a fixed, translatable message; for exceptions raised bare (empty `str(exc)`) |
| `register_handlers(api, HANDLERS)` | Installs an `{exception type: handler}` mapping on the API |

Each app declares a `HANDLERS` table and a `register()` in `<app>/exception_handlers.py`, and calls it from `ready`:

```python
# <app>/exception_handlers.py
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

`api/api.py` stays app-agnostic: it holds only the truly global handlers (`Exception` to 500, `ValidationError` to 400). Dispatch is by MRO, so the most specific registered class wins, and a subclass of a registered exception is covered too. A new app with custom exceptions adds its own `exception_handlers.py` instead of editing `api/api.py`.

### User-facing error strings

Any message that reaches the client (`raise HttpError(<status>, msg)`, or `raise <AppError>(msg)` for an exception mapped with `make_simple_handler`) must be marked for translation with `str(_("..."))`. For interpolation, translate first and then `.format(...)`; f-strings are invisible to `xgettext` and are rejected:

```python
raise HttpError(404, str(_("Unknown event {}.").format(event_id)))
```

`make i18n-literals` (`scripts/check_i18n_literals.py`, also run in CI) enforces this.

## Pagination

Use `PageNumberPaginationExtra` for paginated list endpoints:

```python
from ninja_extra.pagination import PageNumberPaginationExtra


@api_controller("/api/events", auth=I18nJWTAuth(), throttle=UserDefaultThrottle())
class EventController(ControllerBase):
    @route.get("", response=list[EventSchema])
    @paginate(PageNumberPaginationExtra)
    def list_events(self) -> QuerySet[Event]:
        return Event.objects.filter(is_published=True)
```

## Search

Use the `@searching` decorator to add search capabilities:

```python
@route.get("", response=list[EventSchema])
@paginate(PageNumberPaginationExtra)
@searching(Searching, search_fields=["name", "description"])
def list_events(self, filters: t.Annotated[EventFilterSchema, Query(...)]) -> QuerySet[Event]:
    return Event.objects.filter(is_published=True)
```

## Schema Conventions

### ModelSchema: Let It Infer

!!! tip "Only declare fields that need special handling"
    `ModelSchema` infers field types from the model. Only override fields that need transformation (e.g., enum to string).

=== "Good"

    ```python
    class UserSchema(ModelSchema):
        restriction_type: DietaryRestriction.RestrictionType  # (1)!

        class Meta:
            model = User
            fields = ["id", "name", "email"]
    ```

    1. Enum field declared explicitly because it needs the model's enum class for OpenAPI docs.

=== "Bad"

    ```python
    class UserSchema(ModelSchema):
        id: UUID4  # Redundant: ModelSchema infers this
        name: str  # Redundant
        email: str  # Redundant
        created_at: datetime.datetime  # Not needed for API responses

        class Meta:
            model = User
            fields = ["id", "name", "email", "created_at"]
    ```

### Enum Fields

!!! warning "Always use the model's enum class"
    Never use bare `str` or `Literal[...]` for enum fields. This ensures the OpenAPI spec documents valid values and keeps a single source of truth.

```python
# Good - enum referenced from the model
class BannerSchema(Schema):
    severity: SiteSettings.BannerSeverity


# Bad - bare str loses enum contract
class BannerSchema(Schema):
    severity: str


# Bad - Literal duplicates the enum values
class BannerSchema(Schema):
    severity: Literal["info", "warning", "error"]
```

### Datetime Fields

!!! warning "Always use `AwareDatetime`"
    Use `pydantic.AwareDatetime` for datetime fields, never `datetime.datetime`. This enforces timezone-aware serialization.

```python
from pydantic import AwareDatetime


# Good
class EventSchema(Schema):
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None


# Bad - datetime.datetime loses timezone enforcement
class EventSchema(Schema):
    starts_at: datetime.datetime
```

### `Query` and `Body` Parameters

Put the real type in the annotation and the `Query()`/`Body()` object in the `Annotated` metadata. This passes `mypy --strict` without a `# type: ignore[type-arg]` and keeps the OpenAPI constraints:

```python
filters: t.Annotated[EventFilterSchema, Query(...)]  # required: no "= default"
month: t.Annotated[int | None, Query(ge=1, le=12)] = None
```

### Optional JSON Body

!!! note "Django Ninja quirk"
    For endpoints with an optional JSON body, keep `Body(None)` in the metadata via the `Annotated` idiom. Plain `Schema | None = None` causes "Cannot parse request body" when no JSON is sent.

```python
import typing as t

from ninja import Body


@route.post("/confirm")
def confirm(self, payload: t.Annotated[ConfirmSchema | None, Body(None)] = None) -> Response: ...
```

## Query Optimization

!!! tip "Prefetch relationships to avoid N+1 queries"
    When a schema includes nested relationships, ensure the queryset prefetches them.

```python
@route.get("", response=list[EventDetailSchema])
@paginate(PageNumberPaginationExtra)
def list_events(self) -> QuerySet[Event]:
    return (
        Event.objects.select_related("organization", "venue").prefetch_related(  # FK lookups
            "tags", "ticket_tiers"
        )  # Reverse/M2M
    )
```
