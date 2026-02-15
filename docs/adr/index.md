# Architecture Decision Records

Architecture Decision Records (ADRs) are lightweight documents that capture
significant architectural decisions made during the development of the Revel backend.
Each record describes the **context** that motivated the decision, the **decision**
itself, and its **consequences**.

We use the [MADR](https://adr.github.io/madr/) (Markdown Any Decision Records) format.

---

## Template

When adding a new ADR, use this template:

```markdown
# ADR-NNNN: Title

## Status

Accepted | Proposed | Deprecated | Superseded by [ADR-XXXX]

## Context

What is the issue motivating this decision?

## Decision

What is the change we're proposing or doing?

## Consequences

What becomes easier or harder because of this change?
```

---

## Index

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](0001-hmac-caddy-over-s3.md) | HMAC-Signed URLs with Caddy Instead of S3/MinIO | Accepted |
| [ADR-0002](0002-hybrid-service-architecture.md) | Hybrid Service Architecture (Functions + Classes) | Accepted |
| [ADR-0003](0003-uv-over-pip.md) | UV Over pip for Dependency Management | Accepted |
| [ADR-0004](0004-django-ninja-over-drf.md) | Django Ninja Over Django REST Framework | Accepted |
| [ADR-0005](0005-no-dependency-injection.md) | No Dependency Injection Container | Accepted |
| [ADR-0006](0006-django-lts-policy.md) | Django 5.2 LTS Policy | Accepted |
| [ADR-0007](0007-mo-files-in-git.md) | Compiled Translation Files (.mo) in Git | Accepted |
| [ADR-0008](0008-celery-exception-propagation.md) | Let Celery Exceptions Propagate | Accepted |

---

## Adding a New ADR

1. Determine the next sequential number (e.g., `0009`).
2. Create a new file: `docs/adr/NNNN-short-slug.md`.
3. Use the template above.
4. Set the status to **Proposed** until the team reviews it, then update to
   **Accepted**.
5. Add an entry to the index table in this file.

!!! info "When to Write an ADR"

    Write an ADR when a decision is **significant and hard to reverse**: choosing a
    framework, changing a core pattern, adopting a new tool, or deprecating an
    existing approach. Day-to-day implementation choices do not need ADRs.
