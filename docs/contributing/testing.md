# Testing

Revel uses **pytest** with Django integration as its testing framework.
This guide covers how to write, organize, and run tests.

---

## Running Tests

| Command | Description |
|---|---|
| `make test` | Run the full test suite with coverage reporting |
| `make test-failed` | Re-run only previously failed tests |
| `make test-functional` | Run functional tests only |
| `make test-pipeline` | Run tests with **100% coverage** requirement (used in CI) |

!!! tip

    Use `make test-failed` during development to quickly iterate on broken tests
    without running the entire suite.

---

## Test Organization

The project maintains separate **unit** and **functional** test suites:

- **Unit tests** validate individual functions, services, and models in isolation.
- **Functional tests** validate API endpoints and multi-step workflows end-to-end.

### File Length Limit

!!! warning

    Test files must not exceed **1,000 lines**. This is enforced by `make check`.
    When a test file grows too large, split it into focused modules
    (e.g., `test_ticket_checkout.py`, `test_ticket_checkin.py`).

---

## Writing Tests

### Factory-Based Test Data

Use **factory classes** for test data generation instead of manually constructing
model instances. Factories produce realistic, consistent test data and reduce
boilerplate.

```python
import typing as t

from events.factories import EventFactory, TicketTierFactory
from accounts.factories import UserFactory


class TestTicketCheckout:
    def test_successful_checkout(self, db: t.Any) -> None:
        user = UserFactory()
        event = EventFactory()
        tier = TicketTierFactory(event=event)

        # ... test logic
```

### Type Hints Are Required

!!! warning "Critical Rule"

    All test functions and fixtures must include **type hints**. The project runs
    `mypy --strict`, and tests are not exempt.

```python
import typing as t

import pytest
from django.test import RequestFactory

from accounts.models import RevelUser


@pytest.fixture
def request_factory() -> RequestFactory:
    """Provide a Django RequestFactory instance."""
    return RequestFactory()


def test_user_creation(db: t.Any) -> None:
    user = RevelUser.objects.create_user(
        email="test@example.com",
        password="testpass123",
    )
    assert user.email == "test@example.com"
```

### Mock External Services

Always mock services that reach outside the application boundary:

- **Celery tasks** -- prevent actual async execution
- **Email sending** -- avoid SMTP calls
- **File uploads** -- skip storage backends
- **External APIs** -- Stripe, Telegram, etc.

```python
from unittest.mock import patch


def test_sends_welcome_email(db: t.Any) -> None:
    with patch("accounts.tasks.send_welcome_email.delay") as mock_task:
        # ... trigger the action
        mock_task.assert_called_once_with(user_id=user.id)
```

### Test Both Success and Error Cases

Every feature should have tests covering:

- **Happy path** -- the expected successful outcome
- **Validation errors** -- invalid input, missing fields
- **Permission errors** -- unauthorized access attempts
- **Edge cases** -- empty collections, boundary values, race conditions

```python
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

    The CI pipeline (`make test-pipeline`) enforces **100% coverage**. Any uncovered
    lines will cause the pipeline to fail. Use `# pragma: no cover` sparingly and
    only for genuinely unreachable code paths.

---

## Quick Reference

| Guideline | Details |
|---|---|
| Framework | pytest + Django integration |
| Test data | Factory classes |
| Type hints | Required on all tests and fixtures |
| External services | Always mocked |
| Coverage target | 100% in CI |
| Max file length | 1,000 lines |
| Error cases | Always tested alongside success cases |
