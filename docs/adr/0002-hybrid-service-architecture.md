# ADR-0002: Hybrid Service Architecture (Functions + Classes)

## Status

Accepted

## Context

As the codebase grew, we needed consistent patterns for organizing business logic
outside of models and controllers. Two common approaches exist:

- **Pure function-based services**: Every operation is a standalone function. Simple
  and composable, but leads to repetitive argument passing when multiple functions
  share context (user, event, organization).
- **Pure class-based services**: Every operation lives on a class. Elegant for
  workflows, but over-engineered for simple CRUD and one-off helpers.

Neither approach alone fits all use cases cleanly.

## Decision

Use a **hybrid approach**:

- **Function-based** for stateless operations: CRUD, validation, simple queries,
  utility helpers, and cross-entity operations without shared context.
- **Class-based** for stateful workflows: checkout flows, eligibility checks,
  multi-step processes, and any operation where you would pass the same 3+ arguments
  to multiple related functions.

Mixed modules are explicitly allowed. A single service file can contain both functions
and classes when they serve different purposes within the same domain.

### Examples

**Function-based** (stateless):

```python
def add_to_blacklist(organization: Organization, email: str, ...) -> Blacklist:
    return Blacklist.objects.create(organization=organization, email=email, ...)
```

**Class-based** (stateful workflow):

```python
class BatchTicketService:
    def __init__(self, event: Event, tier: TicketTier, user: RevelUser) -> None:
        self.event = event
        self.tier = tier
        self.user = user

    def create_batch(self, items: list[TicketPurchaseItem]) -> list[Ticket] | str:
        ...
```

## Consequences

**Positive:**

- Right tool for the job: simple operations stay simple, complex workflows get
  proper encapsulation
- Clean, readable code with minimal boilerplate
- Easy to grep for usages (explicit imports, no DI magic)

**Negative:**

- Developers need to understand when to use which pattern
- Not a single universal pattern; requires judgment

**Neutral:**

- Guidelines are documented in `CLAUDE.md` and the
  [Code Style](../contributing/code-style.md) guide
- Code review catches misuse during PR review
