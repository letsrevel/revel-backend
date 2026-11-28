"""Tests for the global API exception handlers in ``api.exception_handlers``.

These exercise the last-resort ``ValidationError`` fallback and the deadlock handler directly, with a
``RequestFactory`` request. The deadlock tests need the database for the transaction state.
"""

import json

import psycopg
import pytest
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import OperationalError, transaction
from django.test import RequestFactory

from api.exception_handlers import handle_django_validation_error, handle_operational_error


def test_handle_django_validation_error_dict_form(rf: RequestFactory) -> None:
    """Field-keyed (``full_clean``-style) errors keep the ``{field: [...]}`` contract."""
    request = rf.post("/anything")
    exc = ValidationError({"name": ["is required", "too short"]})

    response = handle_django_validation_error(request, exc)

    assert response.status_code == 400
    assert json.loads(response.content) == {"errors": {"name": ["is required", "too short"]}}


def test_handle_django_validation_error_string_form(rf: RequestFactory) -> None:
    """A string-form ValidationError must not crash the handler (no ``error_dict``)."""
    request = rf.post("/anything")
    exc = ValidationError("something went wrong")

    response = handle_django_validation_error(request, exc)

    assert response.status_code == 400
    assert json.loads(response.content) == {"errors": {NON_FIELD_ERRORS: ["something went wrong"]}}


def test_handle_django_validation_error_list_form(rf: RequestFactory) -> None:
    """A list-form ValidationError must not crash the handler (no ``error_dict``)."""
    request = rf.post("/anything")
    exc = ValidationError(["first problem", "second problem"])

    response = handle_django_validation_error(request, exc)

    assert response.status_code == 400
    assert json.loads(response.content) == {"errors": {NON_FIELD_ERRORS: ["first problem", "second problem"]}}


def _operational_error(cause: Exception) -> OperationalError:
    error = OperationalError(str(cause))
    error.__cause__ = cause
    return error


@pytest.mark.django_db
def test_a_deadlock_rolls_the_request_transaction_back(rf: RequestFactory) -> None:
    """Ninja handles the error inside the view, so without the mark ``ATOMIC_REQUESTS`` would commit the request's
    earlier writes and run its ``on_commit`` callbacks (#1618)."""
    with transaction.atomic():  # stands for the ATOMIC_REQUESTS block
        response = handle_operational_error(rf.post("/anything"), _operational_error(psycopg.errors.DeadlockDetected()))
        assert transaction.get_rollback() is True

    assert response.status_code == 409
    assert json.loads(response.content)["code"] == "concurrent_change"


@pytest.mark.django_db
def test_another_database_error_does_not_mark_a_rollback(rf: RequestFactory) -> None:
    with transaction.atomic():
        response = handle_operational_error(rf.post("/anything"), _operational_error(psycopg.errors.AdminShutdown()))
        assert transaction.get_rollback() is False

    assert response.status_code == 500
