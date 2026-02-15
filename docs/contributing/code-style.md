# Code Style

This guide covers the coding conventions enforced across the Revel backend.
Following these consistently keeps the codebase readable, type-safe, and easy to maintain.

---

## Typing Imports

!!! warning "Critical Rule"

    Always use `import typing as t`. **Never** use `from typing import ...`.

Access all type constructs through the `t` namespace: `t.Any`, `t.TYPE_CHECKING`,
`t.cast()`, `t.ClassVar`, etc.

=== "Good"

    ```python
    import typing as t

    def process(data: t.Any) -> dict[str, t.Any]:
        if t.TYPE_CHECKING:
            from mymodule import MyType
        return t.cast(dict[str, t.Any], data)
    ```

=== "Bad"

    ```python
    from typing import Any, TYPE_CHECKING, cast

    def process(data: Any) -> dict[str, Any]:
        if TYPE_CHECKING:
            from mymodule import MyType
        return cast(dict[str, Any], data)
    ```

**Rationale:** Consistent namespace, avoids polluting module scope, and makes the
provenance of type constructs immediately clear.

---

## Schemas (Django Ninja)

### ModelSchema: Let Inference Do the Work

Only declare fields at class level when they require special handling (e.g., enum
conversion, custom serialization). Let `ModelSchema` infer types from the model
definition.

=== "Good"

    ```python
    class UserSchema(ModelSchema):
        restriction_type: DietaryRestriction.RestrictionType  # (1)!

        class Meta:
            model = User
            fields = ["id", "name", "email"]
    ```

    1. Only declared because it needs explicit enum typing.

=== "Bad"

    ```python
    class UserSchema(ModelSchema):
        id: UUID4  # (1)!
        name: str
        email: str
        created_at: datetime.datetime

        class Meta:
            model = User
            fields = ["id", "name", "email", "created_at"]
    ```

    1. All of these are redundant -- ModelSchema already infers them.

### Omit Timestamp Fields

Do not include `created_at` / `updated_at` in response schemas unless the client
specifically needs them.

### Enum Fields

!!! warning "Critical Rule"

    Always use the **model's enum class** directly (e.g., `Model.MyEnum`).
    Never use bare `str` or `Literal[...]`.

This ensures the OpenAPI spec documents valid values and keeps a single source of truth.

=== "Good"

    ```python
    class BannerSchema(Schema):
        severity: SiteSettings.BannerSeverity
    ```

=== "Bad"

    ```python
    # Bare str -- loses the enum contract entirely
    class BannerSchema(Schema):
        severity: str

    # Literal -- duplicates enum values, drifts over time
    class BannerSchema(Schema):
        severity: Literal["info", "warning", "error"]
    ```

### Datetime Fields

!!! warning "Critical Rule"

    Always use `pydantic.AwareDatetime` for datetime fields.
    Never use `datetime.datetime`.

This enforces timezone-aware serialization in the OpenAPI spec and consistent API
contracts.

=== "Good"

    ```python
    from pydantic import AwareDatetime

    class EventSchema(Schema):
        starts_at: AwareDatetime
        ends_at: AwareDatetime | None = None
    ```

=== "Bad"

    ```python
    import datetime

    class EventSchema(Schema):
        starts_at: datetime.datetime  # No timezone enforcement
    ```

---

## Controllers (Django Ninja Extra)

### Authentication and Throttling

Define `auth` and `throttle` at the **class level** when all endpoints share the same
configuration. Override per-endpoint only when needed.

| Endpoint Type | Throttle |
|---|---|
| `GET` (read) | `UserDefaultThrottle` (class default) |
| `POST` / `PATCH` / `DELETE` (mutation) | `WriteThrottle()` per-endpoint |

### Flat Functions with Early Returns

Keep controller methods flat. Avoid nested conditionals and try-except blocks.

=== "Good"

    ```python
    @api_controller("/api", auth=I18nJWTAuth(), throttle=UserDefaultThrottle())
    class MyController(ControllerBase):
        @route.post("/items", throttle=WriteThrottle())
        def create_item(self, payload: ItemCreateSchema) -> Item:
            return Item.objects.create(**payload.model_dump())

        @route.patch("/items/{id}", throttle=WriteThrottle())
        def update_item(self, id: UUID, payload: ItemUpdateSchema) -> Item:
            item = get_object_or_404(Item, id=id, user=self.user())
            update_data = payload.model_dump(exclude_unset=True)
            if not update_data:
                return item
            for field, value in update_data.items():
                setattr(item, field, value)
            item.save(update_fields=list(update_data.keys()))
            return item
    ```

=== "Bad"

    ```python
    @api_controller("/api", throttle=AuthThrottle())
    class MyController(ControllerBase):
        @route.post("/items", auth=I18nJWTAuth(), throttle=WriteThrottle())  # (1)!
        def create_item(self, payload: ItemCreateSchema) -> Item:
            return Item.objects.create(**payload.model_dump())

        @route.patch("/items/{id}", auth=I18nJWTAuth(), throttle=WriteThrottle())
        def update_item(self, id: UUID, payload: ItemUpdateSchema) -> Item:
            try:  # (2)!
                item = Item.objects.get(id=id, user=self.user())
            except Item.DoesNotExist:
                raise Http404

            if payload.field1 is not None:  # (3)!
                item.field1 = payload.field1
            if payload.field2 is not None:
                item.field2 = payload.field2
            item.save()
            return item
    ```

    1. Repetitive auth on every endpoint -- should be at class level.
    2. Manual try-except -- use `get_object_or_404` instead.
    3. Nested conditionals -- use `model_dump(exclude_unset=True)` loop.

!!! tip "Lookups"

    Use `get_object_or_404()` or `self.get_object_or_exception()` for object lookups.
    Never write manual try-except blocks for `DoesNotExist`.

---

## Race Condition Protection

!!! warning "Critical Rule"

    Use `get_or_create_with_race_protection()` from `common.utils` for create
    operations with uniqueness constraints. Never wrap `IntegrityError` manually.

=== "Good"

    ```python
    from common.utils import get_or_create_with_race_protection

    food_item, created = get_or_create_with_race_protection(
        FoodItem,
        Q(name__iexact=name),
        {"name": name},
    )
    ```

=== "Bad"

    ```python
    try:
        food_item = FoodItem.objects.create(name=name)
    except IntegrityError:
        food_item = FoodItem.objects.get(name__iexact=name)
    ```

---

## Query Optimization

Prevent N+1 queries by prefetching relationships in querysets, especially when schemas
include nested objects.

| Relationship | Method |
|---|---|
| Foreign key (forward) | `select_related()` |
| Reverse FK / Many-to-Many | `prefetch_related()` |

```python
# Controller with proper prefetching
def list_items(self) -> QuerySet[Item]:
    return (
        Item.objects
        .select_related("category")
        .prefetch_related("tags")
    )
```

!!! tip

    When a `ModelSchema` includes nested relationships, always verify the queryset
    prefetches them. [Django Silk](https://github.com/jazzband/django-silk) (enabled with `SILK_PROFILER=True`) or `django.db.connection.queries` can help
    identify N+1 issues during development.

---

## Service Layer Patterns

The project uses a **hybrid approach**: function-based for stateless operations,
class-based for stateful workflows. See [ADR-0002](../adr/0002-hybrid-service-architecture.md)
for the full rationale.

### Function-Based Services

Use for single-purpose operations, cross-entity operations, and utility helpers.

```python
def add_to_blacklist(organization: Organization, email: str, ...) -> Blacklist:
    return Blacklist.objects.create(organization=organization, email=email, ...)

def create_venue(organization: Organization, payload: VenueCreateSchema) -> Venue:
    return Venue.objects.create(organization=organization, **payload.model_dump())
```

### Class-Based Services

Use for request-scoped workflows, multi-step processes, and stateful computations.

```python
class BatchTicketService:
    def __init__(self, event: Event, tier: TicketTier, user: RevelUser) -> None:
        self.event = event
        self.tier = tier
        self.user = user

    def create_batch(self, items: list[TicketPurchaseItem]) -> list[Ticket] | str:
        # Validates batch size, resolves seats, delegates to payment flow
        ...
```

### Controller Integration

```python
# Function-based -- import module, call directly
from events.service import blacklist_service
entry = blacklist_service.add_to_blacklist(organization, email=email)

# Class-based -- instantiate per request
from events.service.batch_ticket_service import BatchTicketService
service = BatchTicketService(event=event, tier=tier, user=user)
result = service.create_batch(items=purchase_items)
```

!!! info "Mixed Modules Are Fine"

    A single service module can contain both patterns. For example,
    `batch_ticket_service.py` has `BatchTicketService` (ticket purchase workflow) while
    `ticket_service.py` has `check_in_ticket()` (standalone operation). This is intentional --
    do not force everything into one pattern.
