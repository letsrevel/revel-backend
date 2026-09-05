"""Map ``IntegrationError`` to a JSON body carrying the stable error code (spec §9)."""

import typing as t

from django.http import HttpRequest
from ninja.responses import Response

from common.exception_handlers import ExceptionHandler, register_handlers
from integrations.exceptions import IntegrationError


def _integration_error_handler(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Render IntegrationError as {detail, code, provider_message} JSON with the error's status."""
    err = t.cast(IntegrationError, exc)
    return Response(
        status=err.status,
        data={"detail": err.message, "code": err.code.value, "provider_message": err.provider_message},
    )


HANDLERS: dict[type[Exception], ExceptionHandler] = {IntegrationError: _integration_error_handler}


def register() -> None:
    """Install the integrations handlers on the global Ninja API (called from ``AppConfig.ready``)."""
    from api.api import api

    register_handlers(api, HANDLERS)
