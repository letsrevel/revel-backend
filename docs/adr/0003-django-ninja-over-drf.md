# ADR-0003: Django Ninja Over Django REST Framework

## Status

Accepted

## Context

The project needed a modern REST API framework for Django. We evaluated the two
primary options:

**Django REST Framework (DRF)**:

- Mature, battle-tested, enormous ecosystem
- Verbose serializer definitions with manual field declarations
- Heavy serialization overhead for large payloads
- Separate schema generation tools (drf-spectacular) needed for OpenAPI

**Django Ninja**:

- Newer, inspired by FastAPI's developer experience
- Type-hint driven schemas using Pydantic
- Built-in OpenAPI documentation generation
- Significantly less boilerplate

## Decision

Use **Django Ninja** (with **Django Ninja Extra** for controller-based organization)
as the API layer.

Controllers are organized using the `@api_controller` decorator pattern with
consistent authentication, throttling, and permission handling at the class level.

## Consequences

**Positive:**

- **Type-hint driven schemas**: Pydantic models replace verbose serializer classes,
  with automatic validation, documentation, and the full power of Pydantic's
  ecosystem (custom validators, computed fields, JSON Schema generation)
- **Automatic OpenAPI docs**: No additional tooling needed -- schemas, examples, and
  endpoints are documented from code
- **Performance**: Faster request/response cycle than DRF for typical workloads
- **Less boilerplate**: Schema definitions are concise and DRY
- **Async support**: Native async view support when needed

**Negative:**

- **Smaller ecosystem**: Fewer third-party packages compared to DRF
- **Less community content**: Fewer tutorials, Stack Overflow answers, and blog posts
- **Rapidly evolving**: API surface may change between versions

**Neutral:**

- Different mental model for developers coming from DRF, but similar enough to learn
  quickly
- Django Ninja Extra adds controller-based organization that feels familiar to
  developers from other frameworks (Spring, NestJS, .NET)
