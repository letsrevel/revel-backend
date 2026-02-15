# Service Layer

Revel uses a **hybrid approach** to services: function-based for stateless operations, class-based for stateful workflows. This is an intentional design decision -- not every service needs to be a class, and not every operation needs to be a standalone function.

!!! info "Related decisions"
    See [ADR-0002: Hybrid Service Architecture](../adr/0002-hybrid-service-architecture.md) and [ADR-0004: No Dependency Injection](../adr/0004-no-dependency-injection.md) for the rationale behind this approach.

## Architecture Overview

```mermaid
flowchart TD
    Controller["Controller (API Layer)"]
    Controller -->|"stateless ops"| FuncService["Function-based Service"]
    Controller -->|"stateful workflows"| ClassService["Class-based Service"]

    FuncService --> Models["Django Models / ORM"]
    ClassService --> Models

    FuncService -->|"e.g."| BS["blacklist_service.add_to_blacklist()"]
    FuncService -->|"e.g."| VS["venue_service.create_venue()"]
    ClassService -->|"e.g."| TS["BatchTicketService.create_batch()"]
    ClassService -->|"e.g."| ES["EligibilityService.check_eligibility()"]

    Models --> DB[(PostgreSQL)]
    ClassService -->|"may call"| CeleryTasks["Celery Tasks"]
    FuncService -->|"may call"| CeleryTasks
```

## Choosing the Right Pattern

=== "Function-based Services"

    Use functions when the operation is **stateless** and **single-purpose**.

    **When to use:**

    - Single-purpose operations: CRUD, validation, simple queries
    - Cross-entity operations spanning multiple models without shared context
    - Utility helpers: formatting, calculations, lookups

    ```python
    # Good - stateless operations as functions
    def add_to_blacklist(organization: Organization, email: str, ...) -> Blacklist:
        return Blacklist.objects.create(organization=organization, email=email, ...)

    def create_venue(organization: Organization, payload: VenueCreateSchema) -> Venue:
        return Venue.objects.create(organization=organization, **payload.model_dump())
    ```

    **Characteristics:**

    - No shared state between calls
    - Each function receives everything it needs via arguments
    - Easy to test in isolation
    - Import the module, call the function

=== "Class-based Services"

    Use classes when the operation is **stateful** and involves **multi-step workflows**.

    **When to use:**

    - Request-scoped workflows sharing context (user, event, organization)
    - Multi-step processes: checkout flows, eligibility checks, complex validations
    - Stateful computations: when you'd pass the same 3+ arguments to multiple related functions

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

    **Characteristics:**

    - Shared context via `__init__` avoids passing the same arguments everywhere
    - Private methods (`_stripe_checkout`) encapsulate implementation details
    - Instantiated per-request -- never a singleton
    - Makes complex workflows readable and maintainable

## Mixed Modules Are OK

!!! tip "A single service module can contain both patterns"
    `ticket_service.py` has standalone functions like `check_in_ticket()` while `batch_ticket_service.py` has the class-based `BatchTicketService`. This is intentional -- don't force everything into one pattern.

```python
# batch_ticket_service.py contains the class-based service
# ticket_service.py contains standalone functions like:

def check_in_ticket(
    event: Event, ticket_id: UUID, checked_in_by: RevelUser,
    price_paid: Decimal | None = None,
) -> Ticket:
    """Standalone operation -- no shared state needed."""
    ...

def confirm_ticket_payment(ticket: Ticket, price_paid: Decimal | None = None) -> Ticket:
    """Another standalone operation."""
    ...
```

## Controller Integration

```python
# Function-based - import module, call directly
from events.service import blacklist_service

entry = blacklist_service.add_to_blacklist(organization, email=email)
```

```python
# Class-based - instantiate per request
from events.service.batch_ticket_service import BatchTicketService

service = BatchTicketService(event=event, tier=tier, user=user)
result = service.create_batch(items=purchase_items)
```

!!! note "Module imports vs class imports"
    For function-based services, import the **module** (not the function) to keep call sites explicit: `blacklist_service.add_to_blacklist()` is clearer than a bare `add_to_blacklist()`.

## Why Not Dependency Injection?

!!! warning "Intentional omission"
    We intentionally avoid Django Ninja Extra's DI container. Here's why:

| Concern | Our approach | DI container |
|---|---|---|
| **Request context** | Services need user, event, org -- instantiated per request | DI containers favour singletons or request-scoped factories |
| **Traceability** | `grep BatchTicketService` finds all usages | DI registration is indirect and harder to trace |
| **Framework coupling** | Zero lock-in beyond Django Ninja | Tied to the DI framework's lifecycle |
| **Complexity** | Manual instantiation is simple and clear | Registration, resolution, scoping add cognitive overhead |

The KISS principle wins here. Explicit instantiation is one line of code and makes the dependency graph obvious.

## Key Guidelines

!!! danger "Business logic belongs in services, not controllers or models"
    Controllers should be thin -- validate input, call a service, return output. Models should define data and relationships. Services contain the business rules.

!!! tip "Let exceptions propagate"
    Do not catch exceptions for the sake of catching them. Especially in Celery tasks, it may be important to let them propagate instead of having the tasks fail silently.
