# Controllers

Revel uses [Django Ninja Extra](https://django-ninja-extra.readthedocs.io/) controllers to organize API endpoints into cohesive, class-based units. Controllers handle request parsing, authentication, throttling, and response serialization -- but **not** business logic.

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
| `POST` | `WriteThrottle` | Yes -- per-endpoint |
| `PATCH` | `WriteThrottle` | Yes -- per-endpoint |
| `DELETE` | `WriteThrottle` | Yes -- per-endpoint |

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

!!! danger "No try-except in views"
    Never wrap ORM lookups in try/except blocks. Use `get_object_or_404()` or `self.get_object_or_exception()` instead. Let Django Ninja's exception handlers do their job.

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
def list_events(self, filters: EventFilterSchema = Query(...)) -> QuerySet[Event]:  # type: ignore[type-arg]
    return Event.objects.filter(is_published=True)
```

## Schema Conventions

### ModelSchema -- Let It Infer

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
        id: UUID4           # Redundant -- ModelSchema infers this
        name: str           # Redundant
        email: str          # Redundant
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

### Optional JSON Body

!!! note "Django Ninja quirk"
    For endpoints with an optional JSON body, use `Body(None)` with a type ignore comment. Plain `Schema | None = None` causes "Cannot parse request body" when no JSON is sent.

```python
from ninja import Body

@route.post("/confirm")
def confirm(self, payload: ConfirmSchema | None = Body(None)) -> Response:  # type: ignore[type-arg]
    ...
```

## Query Optimization

!!! tip "Prefetch relationships to avoid N+1 queries"
    When a schema includes nested relationships, ensure the queryset prefetches them.

```python
@route.get("", response=list[EventDetailSchema])
@paginate(PageNumberPaginationExtra)
def list_events(self) -> QuerySet[Event]:
    return (
        Event.objects
        .select_related("organization", "venue")  # FK lookups
        .prefetch_related("tags", "ticket_tiers")  # Reverse/M2M
    )
```
