"""Tests for the shared exception-handler helpers in ``common.exception_handlers``.

These helpers are pure functions over Django/ninja primitives — no database
access required.
"""

import json
import typing as t

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from common.exception_handlers import (
    format_validation_error,
    make_simple_handler,
    make_static_handler,
    register_handlers,
)

# The handlers ignore the request, so a typed ``None`` keeps mypy happy.
_REQUEST = t.cast(HttpRequest, None)


def test_format_validation_error_uses_message_dict() -> None:
    exc = DjangoValidationError({"name": ["is required", "too short"]})
    assert format_validation_error(exc) == "name: is required, too short"


def test_format_validation_error_uses_messages_list() -> None:
    exc = DjangoValidationError(["first", "second"])
    assert format_validation_error(exc) == "first; second"


def test_format_validation_error_falls_back_to_str_for_plain_exception() -> None:
    assert format_validation_error(ValueError("boom")) == "boom"


def test_make_simple_handler_renders_str_of_exception_with_status() -> None:
    handler = make_simple_handler(422)
    response = handler(_REQUEST, ValueError("nope"))
    assert response.status_code == 422
    assert json.loads(response.content) == {"detail": "nope"}


def test_make_simple_handler_formats_validation_error_content() -> None:
    handler = make_simple_handler(400)
    response = handler(_REQUEST, DjangoValidationError({"field": ["bad"]}))
    assert response.status_code == 400
    assert json.loads(response.content) == {"detail": "field: bad"}


def test_make_static_handler_renders_fixed_message_ignoring_exception() -> None:
    handler = make_static_handler(403, "forbidden")
    response = handler(_REQUEST, ValueError("ignored details"))
    assert response.status_code == 403
    assert json.loads(response.content) == {"detail": "forbidden"}


def test_make_static_handler_stringifies_lazy_translation() -> None:
    handler = make_static_handler(400, _("Already a member."))
    response = handler(_REQUEST, Exception())
    assert response.status_code == 400
    assert json.loads(response.content) == {"detail": "Already a member."}


def test_register_handlers_registers_every_mapping() -> None:
    class RecordingApi:
        def __init__(self) -> None:
            self.registered: dict[type[Exception], t.Callable[..., t.Any]] = {}

        def add_exception_handler(self, exc_class: type[Exception], handler: t.Callable[..., t.Any]) -> None:
            self.registered[exc_class] = handler

    api = RecordingApi()
    simple = make_simple_handler(400)
    static = make_static_handler(404, "missing")

    register_handlers(api, {ValueError: simple, KeyError: static})

    assert api.registered == {ValueError: simple, KeyError: static}
