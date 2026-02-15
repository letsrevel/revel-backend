# Project Structure

This page provides a map of the Revel backend codebase -- where things live, why they are organized the way they are, and how the pieces fit together.

## Top-Level Directory

```
revel-backend/
├── src/                    # All application source code
│   ├── revel/              # Django project settings
│   ├── accounts/           # User management app
│   ├── events/             # Core event management app
│   ├── questionnaires/     # Dynamic questionnaires app
│   ├── notifications/      # Multi-channel notifications app
│   ├── wallet/             # Apple Wallet pass generation
│   ├── geo/                # Geolocation app
│   ├── telegram/           # Telegram bot app
│   ├── api/                # Global API configuration
│   └── common/             # Shared utilities
├── docs/                   # MkDocs documentation
├── pyproject.toml          # Dependencies and tool config
├── Makefile                # Development commands
├── compose.yaml            # Docker services (local dev)
└── uv.lock                 # Locked dependency versions
```

## Application Breakdown

### `revel/` -- Django Project Core

The Django project package containing settings, root URL configuration, WSGI/ASGI entry points, and Celery app initialization.

Settings are modular, split across multiple files under `revel/settings/` for clarity and environment-specific overrides.

---

### `accounts/` -- Users and Authentication

Handles everything related to users: registration, login, JWT token management, profile updates, and GDPR compliance (data export and deletion).

Key responsibilities:

- Custom user model (`RevelUser`)
- JWT authentication flow
- User registration and profile management
- GDPR data export and account deletion

---

### `events/` -- Organizations, Events, and Tickets

The largest and most central app. Manages the full lifecycle of events, from organization setup to ticket checkout and check-in.

Key responsibilities:

- **Organizations** -- creation, membership, roles (Owner, Staff, Member)
- **Events** -- creation, configuration, publishing
- **Ticket tiers** -- pricing, capacity, payment methods (free, online, PWYC)
- **Tickets** -- checkout, payment confirmation, check-in
- **Venues** -- location management
- **Invitations** -- invite-based access control
- **Blacklists** -- organization-level email blocking
- **Followers** -- user-to-organization following

---

### `questionnaires/` -- Dynamic Questionnaires

A flexible questionnaire system that can be attached to events for eligibility screening or data collection. Supports LLM-powered evaluation of open-ended responses.

Key responsibilities:

- Questionnaire and question modeling
- Answer submission and validation
- LLM-based evaluation of text answers

---

### `notifications/` -- Multi-Channel Notifications

Delivers notifications to users across multiple channels: in-app, email, and Telegram.

Key responsibilities:

- Notification creation and delivery
- Channel routing (in-app, email, Telegram)
- User notification preferences

---

### `wallet/` -- Apple Wallet Integration

Generates Apple Wallet passes (`.pkpass` files) for event tickets, allowing attendees to add their tickets to the iOS Wallet app.

---

### `geo/` -- Geolocation

Provides geolocation features including city search, IP-based location detection, and geographic data management.

Key responsibilities:

- City data loading and search
- IP-to-location lookup
- PostGIS integration for spatial queries

---

### `telegram/` -- Telegram Bot

A Telegram bot integration with FSM (Finite State Machine) based conversation flows for user interaction and notification delivery.

Key responsibilities:

- Webhook handling for Telegram updates
- FSM-based conversation management
- Account linking (Telegram user to Revel user)
- Notification delivery via Telegram

---

### `api/` -- Global API Configuration

Houses API-wide concerns: the root API instance, global exception handlers, rate limiting configuration, and middleware.

---

### `common/` -- Shared Utilities

A library of reusable components used across all apps.

Key contents:

- Base model classes (timestamped models, UUID primary keys)
- Authentication backends and helpers
- Custom admin site configuration
- Utility functions (race condition protection, etc.)
- Shared schema base classes

## The Controller / Service / Model Pattern

Each app follows a consistent internal structure that separates concerns into three layers:

```
app_name/
├── models/             # Django models -- data and schema
│   ├── __init__.py
│   └── my_model.py
├── controllers/        # API endpoints -- HTTP layer
│   ├── __init__.py
│   └── my_controller.py
├── service/            # Business logic -- orchestration
│   ├── __init__.py
│   └── my_service.py
├── schema/             # Request/response schemas (Django Ninja)
│   ├── __init__.py
│   └── my_schema.py
├── factories.py        # Test data factories
├── admin.py            # Django admin registration
└── tests/              # Unit and integration tests
```

| Layer | Responsibility | Depends On |
|-------|---------------|------------|
| **Controllers** | Parse HTTP requests, call services, return responses | Services, Schemas |
| **Services** | Business logic, validation, orchestration | Models |
| **Models** | Data definition, database schema, querysets | Nothing |
| **Schemas** | Request/response serialization (Django Ninja) | Models (for `ModelSchema`) |

!!! tip "Where does business logic go?"
    Business logic belongs in the **service layer**, not in controllers or models.
    Controllers should be thin -- validate input, call a service, return the result.
    Models should define data and relationships, not orchestrate workflows.

## Key Configuration Files

| File | Purpose |
|------|---------|
| `pyproject.toml` | Dependencies, ruff/mypy/pytest configuration |
| `Makefile` | All development commands and workflows |
| `uv.lock` | Locked dependency versions for reproducible installs |
| `src/revel/settings/` | Modular Django settings (base, local, CI, production) |
| `mkdocs.yml` | Documentation site configuration |

## Docker Compose Files

The project uses multiple Docker Compose files for different environments:

| File | Purpose | Services |
|------|---------|----------|
| `docker-compose-base.yml` | Shared service definitions | PostgreSQL/PostGIS, Redis, MinIO |
| `compose.yaml` | Local development | Extends base + adds Mailpit |
| `docker-compose-ci.yml` | CI/CD pipelines | Minimal services for testing |
| `docker-compose-observability.yml` | Observability stack (optional) | Monitoring and tracing tools |

!!! info "Extending compose files"
    `compose.yaml` and `docker-compose-ci.yml` both extend `docker-compose-base.yml`.
    You only need to run one of them -- `compose.yaml` for local development, or the CI variant in pipelines.
