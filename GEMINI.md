
# Gemini Project Overview

This document provides a general overview of the Revel project, its structure, and conventions to guide future development and ensure consistency.

## Project Overview

Revel is a Django-based web application with a RESTful API built using `django-ninja`. It utilizes a PostgreSQL database, Redis for caching and Celery task queuing, and Minio for object storage. The project is containerized using Docker.

The application is divided into several Django apps: `accounts`, `api`, `common`, `events`, `questionnaires`, and `telegram`. Each app has a specific responsibility and follows a consistent structure.

## Getting Started

To set up the development environment, run the following command:

```bash
make setup
```

This command will:
1. Create a Python virtual environment.
2. Install the required dependencies using `uv`.
3. Copy the example environment file.
4. Start the required services using Docker Compose.
5. Generate test JWTs.
6. Run the Django development server.

## Coding Style and Conventions

The project follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html).

- **Formatting**: The project uses `ruff` for code formatting. To format the code, run `make format`.
- **Linting**: The project uses `ruff` for linting. To lint the code, run `make lint`.
- **Type Hinting**: The project uses `mypy` for static type checking. To run `mypy`, use `make mypy`. All new code should be fully type-hinted.
- **Docstrings**: The project uses Google-style docstrings. All public modules, functions, classes, and methods should have docstrings.
- **Naming Conventions**:
    - Modules: `lowercase_with_underscores`
    - Classes: `PascalCase`
    - Functions: `lowercase_with_underscores`
    - Variables: `lowercase_with_underscores`
    - Constants: `UPPERCASE_WITH_UNDERSCORES`

### General instructions

- the source code is in the `src` directory.
- Before doing anything, read ALL the source code in src and keep it in your context. ALWAYS read the whole code before starting a new action.
- All code should be type hinted, including in tests and mock.
- The usage of `type: ignore[specifier]` is allowed when it makes sense. Use it with caution. Take inspiration by other files.
- YOU MUST NEVER USE `# type: ignore[no-untyped-def]`. Function definitions MUST ALWAYS BE TYPED.
- I prefer not to import annotation from __future__. You can use string-like annotations instead. For example `-> QuerySet["MyModel"]`
- All the functions must follow the Single Responsibility Principle as much as possible. Deviate from it when it makes sense.
- KISS and DRY.
- Use OOP when it makes sense.
- Use best software architecture practices.
- Once you finish writing code, run `make check` and `make test` before writing new tests to test the new code.
- Then run `make check` and `make test` again to ensure everything is correct.

AND MOST IMPORTANTLY: CLEAN CODE.

## Running Tests

The project has a comprehensive test suite using `pytest`. To run the tests, use the following command:

```bash
make test
```

This command will run all the tests and generate a coverage report.

If one or more tests fail, before changing the code, investigate whether the error is due to a faulty test or a faulty implementation.

### Writing new tests

- use pytest.
- It is important that the tests are typed as well.
- Do not write or use fixtures without typing the function signatures, or I will switch to the competition.
- The tests should aim at maximum coverage.
- Each test should test one thing, except when it makes sense to test a whole flow.
- Use fixtures smartly to avoid repeating yourself.

## Important Commands

- `make check`: Runs formatting, linting, and type checking.
- `make run`: Starts the Django development server.
- `make run-celery`: Starts the Celery worker.
- `make run-celery-beat`: Starts the Celery beat scheduler.
- `make migrations`: Creates new database migrations.
- `make migrate`: Applies database migrations.
- `make shell`: Opens the Django shell.
- `make restart`: Restarts the development environment and recreates the database.
- `make nuke-db`: Deletes the database and migrations.

Once you finish writing code, always run `make check` and `make test` to ensure everything is working.

## GitHub CLI

You are only allowed to use the gh cli to create and read issues, never to perform any other actions, unless specifically instructed.

To create a new issue, use:

```
gh issue create --title 'Title' --body $'Body of the issue' --assignee @me
```

Always use `--assignee @me`

When appropriate, use `--label`

Use always single quotation and prefix the body with a $ to make the newlines work. This applies to everything. Also PR commands.

Do not close issues without my explicit command and consent.

## Project Structure

The project is organized into the following directories:

- `src`: Contains the Django project and apps.
- `functional_tests`: Contains functional tests.
- `docs`: Contains the project documentation.

Each Django app in the `src` directory follows a similar structure:

- `models.py`: Contains the Django models.
- `admin.py`: Contains the Django admin configuration.
- `controllers/`: Contains the API endpoints.
- `service/`: Contains the business logic.
- `tests/`: Contains the tests for the app.
