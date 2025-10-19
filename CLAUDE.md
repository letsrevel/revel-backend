# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

This project uses a comprehensive Makefile for development tasks:

### Primary Development Commands
- `make setup` - Complete one-time setup: creates venv, installs dependencies, sets up Docker services, and starts the server
- `make run` - Start Django development server (generates test JWTs first)
- `make jwt EMAIL=user@example.com` - Get JWT access and refresh tokens for a specific user
- `make check` - Run all code quality checks (format, lint, mypy)
- `make test` - Run pytest test suite with coverage reporting
- `make test-failed` - Re-run only failed tests

### Code Quality & Formatting
- `make format` - Auto-format code with ruff
- `make lint` - Run linting with ruff (auto-fixes issues)
- `make mypy` - Run strict type checking with mypy

### Database Management
- `make migrations` - Create new Django migrations
- `make migrate` - Apply pending migrations
- `make bootstrap` - Initialize database with base data
- `make seed` - Populate database with test data
- `make nuke-db` - **DESTRUCTIVE**: Delete database and migration files
- `make restart` - **DESTRUCTIVE**: Restart Docker and recreate database

### Background Services
- `make run-celery` - Start Celery worker for background tasks
- `make run-celery-beat` - Start Celery beat scheduler
- `make run-flower` - Start Flower (Celery monitoring UI)

### Testing Variants
- `make test-functional` - Run functional tests
- `make test-pipeline` - Run tests with 100% coverage requirement

## Project Architecture

Revel is a Django-based event management platform with the following structure:

### Core Applications
- **accounts/** - User authentication, registration, JWT handling, GDPR compliance
- **events/** - Core event management, organizations, tickets, memberships, invitations
- **questionnaires/** - Dynamic questionnaire system with LLM-powered evaluation
- **geo/** - Geolocation features, city data, IP-based location detection
- **telegram/** - Telegram bot integration with FSM-based conversation flows
- **api/** - Global API configuration, exception handlers, rate limiting
- **common/** - Shared utilities, authentication, base models, admin customizations

### Key Architectural Patterns

#### Service Layer Pattern
Business logic is encapsulated in service modules:
- `events/service/` - Event management, organization services
- `accounts/service/` - User management, authentication services
- `questionnaires/service.py` - Questionnaire evaluation logic

#### Controller Pattern (Django Ninja)
API endpoints are organized in controller classes:
- Controllers inherit from `UserAwareController` for common user/auth functionality
- Use `@api_controller` decorator with consistent tagging
- Implement pagination with `PageNumberPaginationExtra`
- Support search with `@searching` decorator

#### Permission System
- Custom permission classes in `events/permissions.py`
- Organization-based access control with roles (Owner, Staff, Member)
- Event-level permissions with eligibility checking
- JWT-based authentication with optional anonymous access

### Technology Stack
- **Backend**: Django 5+ with Django Ninja for API
- **Database**: PostgreSQL with PostGIS for geo features
- **Async Tasks**: Celery with Redis
- **Authentication**: JWT with custom user model
- **File Storage**: Configurable (local/S3)
- **Containerization**: Docker with docker-compose for development

### Testing Framework
- **pytest** with Django integration
- Factory-based test data generation
- Coverage reporting with HTML output
- Separate functional and unit test suites
- Celery task testing with pytest-celery

### Code Quality Standards
- **Formatting**: ruff (replaces black)
- **Linting**: ruff with Django-specific rules
- **Type Checking**: mypy with strict settings and Django plugin
- **Docstrings**: Google-style format required
- **Coverage**: Aim for high coverage, 100% required in CI pipeline

### Development Environment
- Python 3.13+ required
- **UV for dependency management** (use `uv add`/`uv remove` - NEVER use pip directly)
- Docker for services (PostgreSQL, Redis, MinIO)
- Virtual environment automatically created in `.venv/`

### Key Configuration Files
- `pyproject.toml` - Dependencies, tool configuration (ruff, mypy, pytest)
- `Makefile` - Development commands and workflows
- `docker-compose-dev.yml` - Development services
- `src/revel/settings/` - Modular Django settings

### Security Features
- GDPR compliance with data export/deletion
- File malware scanning with ClamAV
- Advanced user permissions and organization roles
- Questionnaire-based access control
- Secure file handling with quarantine system

## Testing Guidelines

When writing tests:
- Use factory classes for test data generation
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
- Use Django Ninja's automatic OpenAPI documentation features
- Use the context7 MCP to look up documentation

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

## Note to claude
- Do not run tests. Let the user run the tests.
- Always discuss implementation approach before writing code for non-trivial changes.