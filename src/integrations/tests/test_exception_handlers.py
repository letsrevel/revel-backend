"""IntegrationError renders {detail, code, provider_message} with the error's status."""

import orjson
from django.test import RequestFactory

from integrations.exception_handlers import HANDLERS
from integrations.exceptions import IntegrationError
from integrations.schema import IntegrationErrorCode


def test_handler_renders_code_and_provider_message() -> None:
    exc = IntegrationError(
        IntegrationErrorCode.PROVIDER_REJECTED, "Eventbrite rejected the request.", "SUMMARY_TOO_LONG", status=502
    )
    response = HANDLERS[IntegrationError](RequestFactory().get("/"), exc)
    assert response.status_code == 502
    assert orjson.loads(response.content) == {
        "detail": "Eventbrite rejected the request.",
        "code": "provider_rejected",
        "provider_message": "SUMMARY_TOO_LONG",
    }
