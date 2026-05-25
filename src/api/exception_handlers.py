"""Exception handlers for the API."""

import traceback
import typing as t
from copy import deepcopy

import orjson
import structlog
from django.conf import settings
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja.responses import Response

logger = structlog.get_logger(__name__)


def handle_general_exception(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Handle a general exception.

    Logs the exception to the observability stack (Loki) with full context.
    Alerts should be configured in Grafana based on error rates and patterns.

    Args:
        request: The incoming HTTP request.
        exc: The exception.

    Returns:
        The response.
    """
    # Parse JSON payload if present (for better debugging context)
    json_payload = None
    if request.method in ("POST", "PUT", "PATCH") and request.headers.get("Content-Type") == "application/json":
        try:
            json_payload = obfuscate(orjson.loads(request.body))
        except Exception:  # pragma: no cover
            json_payload = None

    # Log to observability stack with full context.
    # Pass the exception instance to ``exc_info`` (not ``True``) so structlog's
    # ``format_exc_info`` processor pulls the traceback from ``exc.__traceback__``.
    # ``exc_info=True`` relies on ``sys.exc_info()`` being populated, which is not
    # guaranteed by the time Ninja's exception dispatch reaches this handler.
    # We also emit the formatted traceback as an explicit ``traceback`` field as a
    # belt-and-braces safety net: if anything downstream drops the ``exception``
    # key (custom processors, renderers, etc.), the stacktrace is still visible.
    exc_instance = exc if isinstance(exc, BaseException) else None
    formatted_tb = "".join(traceback.format_exception(exc_instance)) if exc_instance else None

    logger.error(
        "unhandled_exception",
        exc_info=exc_instance,
        traceback=formatted_tb,
        method=request.method,
        path=request.path,
        user=str(request.user) if getattr(request, "user", None) else None,
        user_id=str(request.user.id) if getattr(request, "user", None) and hasattr(request.user, "id") else None,
        headers=obfuscate(dict(request.headers)),
        query_params=obfuscate(request.GET.dict()),
        post_params=obfuscate(request.POST.dict() if request.method == "POST" else {}),
        json_payload=json_payload,
        exception_type=type(exc).__name__,
        exception_message=str(exc),
    )

    # Return response
    data = {"detail": str(_("Internal Server Error."))}
    if settings.DEBUG:  # pragma: no cover
        data["traceback"] = traceback.format_exc()

    return Response(status=500, data=data)


def handle_django_validation_error(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Handle a validation error.

    This is the last-resort, app-agnostic fallback for Django ``full_clean``
    violations that no app's own handler claims. App-specific ``ValidationError``
    subclasses self-register in their app and win by MRO before reaching here.

    Args:
        request: The incoming HTTP request.
        exc: The exception.
    """
    validation_error = t.cast(ValidationError, exc)
    if hasattr(validation_error, "error_dict"):
        error_dict = {k: [ee for e in v for ee in e] for k, v in validation_error.error_dict.items()}
    else:
        # String/list-form ValidationErrors carry only ``error_list`` (no
        # ``error_dict``); group their messages under the non-field key so this
        # last-resort handler never raises on a valid ValidationError shape.
        error_dict = {NON_FIELD_ERRORS: list(validation_error.messages)}
    logger.warning(
        "validation_error",
        method=request.method,
        path=request.path,
        user_id=str(request.user.id) if getattr(request, "user", None) and hasattr(request.user, "id") else None,
        errors=error_dict,
    )
    return Response(status=400, data={"errors": error_dict})


SENSITIVE_KEYS = {"password", "token", "x-api-key", "authorization", "authentication"}


def obfuscate(data: dict[str, t.Any]) -> dict[str, t.Any]:
    """Obfuscate sensitive data in payloads and headers."""
    new_data = deepcopy(data)
    for key in data.keys():
        if key.lower() in SENSITIVE_KEYS:
            new_data[key] = "********"
    return new_data
