"""Tests for the global API exception handlers in ``api.exception_handlers``.

These exercise the last-resort ``ValidationError`` fallback directly. No
database access is required, so a ``RequestFactory`` request is enough.
"""

import json

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.test import RequestFactory

from api.exception_handlers import handle_django_validation_error


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
