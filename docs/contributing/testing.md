# Testing

Revel uses **pytest** with Django integration as its testing framework.
This guide covers how to write, organize, and run tests.

---

## Running Tests

| Command | Description |
|---|---|
| `make test` | Run the full test suite in parallel (`pytest -n auto`) with coverage reporting |
| `make test-linear` | Run the full test suite sequentially (single process, useful for debugging) |
| `make test-failed` | Re-run only previously failed tests |

!!! tip

    Use `make test-failed` during development to quickly iterate on broken tests
    without running the entire suite.

---

## Test Organization

Tests live alongside the code they test, inside each app's `tests/` directory.

### File Length Limit

!!! warning

    Test files must not exceed **1,000 lines**. This is enforced by `make check`.
    When a test file grows too large, split it into focused modules
    (e.g., `test_ticket_checkout.py`, `test_ticket_checkin.py`).

---

## Writing Tests

### Test Data

Tests create data directly using Django ORM and shared fixtures from `conftest.py`:

```python
import typing as t

import pytest

from accounts.models import RevelUser


@pytest.mark.django_db
class TestTicketCheckout:
    def test_successful_checkout(self) -> None:
        user = RevelUser.objects.create_user(
            email="test@example.com",
            password="testpass123",
        )
        # ... test logic
```

### Type Hints Are Required

!!! warning "Critical Rule"

    All test functions and fixtures must include **type hints**. The project runs
    `mypy --strict`, and tests are not exempt.

Use the `@pytest.mark.django_db` decorator (class-level or function-level) for tests that need database access:

```python
import pytest
from django.test import RequestFactory


@pytest.fixture
def request_factory() -> RequestFactory:
    """Provide a Django RequestFactory instance."""
    return RequestFactory()


@pytest.mark.django_db
def test_user_creation() -> None:
    user = RevelUser.objects.create_user(
        email="test@example.com",
        password="testpass123",
    )
    assert user.email == "test@example.com"
```

### Auto-Use Fixtures

The root `conftest.py` defines several `autouse=True` fixtures that apply to **all tests** automatically:

| Fixture | Purpose |
|---|---|
| `mock_ip2location` | Mocks IP2Location to avoid `.BIN` file dependency |
| `increase_rate_limit` | Raises rate limits so tests don't get throttled |
| `enable_celery_eager_mode` | Forces Celery tasks to execute synchronously |
| `use_locmem_cache` | Uses in-memory cache instead of Redis (safe for parallel runs) |
| `use_fast_password_hasher` | Uses MD5 hasher for faster user creation in tests |
| `mock_telegram_send_message` | Prevents real Telegram messages from being sent |
| `mock_clamav` | Mocks ClamAV scanning so tests don't need a running daemon |

These fixtures ensure tests are fast, isolated, and don't depend on external services.

### Mock External Services

Always mock services that reach outside the application boundary:

- **Celery tasks**: prevent actual async execution
- **Email sending**: avoid SMTP calls
- **File uploads**: skip storage backends
- **External APIs**: Stripe, Telegram, etc.

```python
from unittest.mock import patch


@pytest.mark.django_db
def test_sends_notification_email() -> None:
    with patch("notifications.tasks.dispatch_notification.delay") as mock_task:
        # ... trigger the action
        mock_task.assert_called_once()
```

### Test Both Success and Error Cases

Every feature should have tests covering:

- **Happy path**: the expected successful outcome
- **Validation errors**: invalid input, missing fields
- **Permission errors**: unauthorized access attempts
- **Edge cases**: empty collections, boundary values, race conditions

```python
@pytest.mark.django_db
class TestEventCreation:
    def test_create_event_success(self, auth_client: t.Any) -> None:
        response = auth_client.post("/api/events/", payload)
        assert response.status_code == 201

    def test_create_event_missing_title(self, auth_client: t.Any) -> None:
        response = auth_client.post("/api/events/", {})
        assert response.status_code == 422

    def test_create_event_unauthorized(self, client: t.Any) -> None:
        response = client.post("/api/events/", payload)
        assert response.status_code == 401
```

---

## Coverage

The test suite generates coverage reports with HTML output. After running
`make test`, open `htmlcov/index.html` in a browser to inspect line-by-line
coverage.

!!! info "CI Requirement"

    The CI pipeline enforces **90%+ branch coverage** via `--cov-fail-under=90`. Any drop below this threshold
    will cause the pipeline to fail. Local `make test` generates a coverage report but does **not** enforce
    the threshold. You will only see a failure in CI. Use `# pragma: no cover` sparingly and
    only for genuinely unreachable code paths.

---

## Quick Reference

| Guideline | Details |
|---|---|
| Framework | pytest + Django integration |
| Test data | Direct ORM creation + conftest fixtures |
| DB access | `@pytest.mark.django_db` decorator |
| Type hints | Required on all tests and fixtures |
| External services | Always mocked |
| Coverage target | 90%+ in CI |
| Max file length | 1,000 lines |
| Error cases | Always tested alongside success cases |
