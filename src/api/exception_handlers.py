"""Exception handlers for the API."""

import traceback
import typing as t
from copy import deepcopy

import orjson
import structlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja.responses import Response

from events.exceptions import (
    AlreadyMemberError,
    PendingMembershipRequestExistsError,
    TooManyItemsError,
)
from events.service.event_manager import UserIsIneligibleError
from questionnaires.exceptions import (
    CrossQuestionnaireSubmissionError,
    FileValidationError,
    MissingMandatoryAnswerError,
    QuestionIntegrityError,
    SectionIntegrityError,
)

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
    is_staff = getattr(request, "user", None) and request.user.is_staff

    # Parse JSON payload if present (for better debugging context)
    json_payload = None
    if request.method in ("POST", "PUT", "PATCH") and request.headers.get("Content-Type") == "application/json":
        try:
            json_payload = obfuscate(orjson.loads(request.body))
        except Exception:  # pragma: no cover
            json_payload = None

    # Log to observability stack with full context
    logger.error(
        "unhandled_exception",
        exc_info=True,
        method=request.method,
        path=request.path,
        user=str(request.user) if getattr(request, "user", None) else None,
        user_id=str(request.user.id) if getattr(request, "user", None) and hasattr(request.user, "id") else None,
        headers=obfuscate(dict(request.headers)),
        query_params=obfuscate(request.GET.dict()),
        post_params=obfuscate(request.POST.dict() if request.method == "POST" else {}),
        json_payload=json_payload,
        exception_type=type(exc).__name__,
    )

    # Return response
    data = {"detail": str(_("Internal Server Error."))}
    if settings.DEBUG or is_staff:  # pragma: no cover
        data["traceback"] = traceback.format_exc()

    return Response(status=500, data=data)


def handle_django_validation_error(request: HttpRequest, exc: ValidationError | t.Type[ValidationError]) -> Response:
    """Handle a validation error.

    Args:
        request: The incoming HTTP request.
        exc: The exception.
    """
    error_dict = {k: [ee for e in v for ee in e] for k, v in exc.error_dict.items()}
    logger.warning(
        "validation_error",
        method=request.method,
        path=request.path,
        user_id=str(request.user.id) if getattr(request, "user", None) and hasattr(request.user, "id") else None,
        errors=error_dict,
    )
    return Response(status=400, data={"errors": error_dict})


def handle_user_is_ineligible_error(
    request: HttpRequest, exc: UserIsIneligibleError | t.Type[UserIsIneligibleError]
) -> Response:
    """Handle a user is-ineligible error."""
    return Response(status=400, data=exc.eligibility.model_dump(mode="json"))


def handle_cross_questionnaire_submission_error(
    request: HttpRequest, exc: CrossQuestionnaireSubmissionError | t.Type[CrossQuestionnaireSubmissionError]
) -> Response:
    """Handle a cross-questionnaire submission error."""
    return Response(status=400, data={"detail": str(_("You submitted answers refer to a different questionnaire."))})


def handle_missing_mandatory_answers_submission_error(
    request: HttpRequest, exc: MissingMandatoryAnswerError | t.Type[MissingMandatoryAnswerError]
) -> Response:
    """Handle a cross-questionnaire submission error."""
    return Response(status=400, data={"detail": str(_("You are missing mandatory answers."))})


def handle_section_integrity_error(
    request: HttpRequest, exc: SectionIntegrityError | t.Type[SectionIntegrityError]
) -> Response:
    """Handle a section integrity error."""
    return Response(status=400, data={"detail": str(exc)})


def handle_question_integrity_error(
    request: HttpRequest, exc: QuestionIntegrityError | t.Type[QuestionIntegrityError]
) -> Response:
    """Handle a question integrity error."""
    return Response(status=400, data={"detail": str(exc)})


def handle_too_many_items_error(request: HttpRequest, exc: TooManyItemsError | t.Type[TooManyItemsError]) -> Response:
    """Handle a too many items error."""
    return Response(status=400, data={"detail": str(_("You have created too many items."))})


def handle_already_member_error(request: HttpRequest, exc: AlreadyMemberError | t.Type[AlreadyMemberError]) -> Response:
    """Handle an already member error."""
    return Response(status=400, data={"detail": str(_("You are already a member of this organization."))})


def handle_pending_membership_request_exists_error(
    request: HttpRequest, exc: PendingMembershipRequestExistsError | t.Type[PendingMembershipRequestExistsError]
) -> Response:
    """Handle a pending membership request exists error."""
    return Response(status=400, data={"detail": str(_("You have a pending membership request for this organization."))})


def handle_file_validation_error(
    request: HttpRequest, exc: FileValidationError | t.Type[FileValidationError]
) -> Response:
    """Handle any file validation error with the exception's message."""
    return Response(status=400, data={"detail": str(exc)})


SENSITIVE_KEYS = {"password", "token", "x-api-key", "authorization", "authentication"}


def obfuscate(data: dict[str, t.Any]) -> dict[str, t.Any]:
    """Obfuscate sensitive data in payloads and headers."""
    new_data = deepcopy(data)
    for key in data.keys():
        if key.lower() in SENSITIVE_KEYS:
            new_data[key] = "********"
    return new_data
