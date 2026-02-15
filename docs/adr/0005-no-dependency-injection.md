# ADR-0005: No Dependency Injection Container

## Status

Accepted

## Context

Django Ninja Extra ships with a built-in dependency injection (DI) container based on
the `injector` library. We evaluated whether to use it for managing service
instantiation and lifecycle.

Key observations:

- Our services are **request-contextual**: they need the current user, event,
  organization, or other per-request data to function. This does not fit the
  singleton or scoped patterns that DI containers excel at.
- The DI container adds an abstraction layer that makes it harder to trace where
  services are instantiated and used.
- The project values **simplicity** (KISS) and **traceability** (grep-friendly code).

## Decision

**Avoid the DI container.** Use explicit instantiation of services throughout the
codebase.

```python
# Function-based -- import the module, call the function
from events.service import blacklist_service
entry = blacklist_service.add_to_blacklist(organization, email=email)

# Class-based -- instantiate per request with context
from events.service.ticket_service import TicketService
service = TicketService(event=event, tier=tier, user=user)
result = service.checkout()
```

## Consequences

**Positive:**

- **Traceable**: `grep` and IDE "Find Usages" work reliably -- no magic resolution
- **No framework lock-in**: Services are plain Python, not tied to Ninja Extra's DI
- **Simple and clear**: Follows KISS -- manual instantiation is easy to understand
- **Natural fit**: Request-contextual services are instantiated where they are used,
  with exactly the data they need

**Negative:**

- **Manual wiring**: No automatic lifecycle management or constructor injection
- **Repetition**: Service instantiation may be repeated in multiple controllers

**Neutral:**

- Services that need user/event/organization context are instantiated per-request
  regardless of whether DI is used -- the container would add indirection without
  reducing code
