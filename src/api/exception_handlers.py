"""Exception handlers for the API."""

import base64
import traceback
import typing as t
from copy import deepcopy

import orjson
import structlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from ninja.responses import Response

from events.exceptions import (
    AlreadyMemberError,
    PendingMembershipRequestExistsError,
    TooManyItemsError,
)
from events.service.event_manager import UserIsIneligibleError
from questionnaires.exceptions import (
    CrossQuestionnaireSubmissionError,
    MissingMandatoryAnswerError,
    QuestionIntegrityError,
    SectionIntegrityError,
)

from .tasks import track_internal_error

logger = structlog.get_logger(__name__)


def handle_general_exception(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Handle a general exception.

    Args:
        request: The incoming HTTP request.
        exc: The exception.

    Returns:
        The response.
    """
    logger.exception("INTERNAL_SERVER_ERROR", exc_info=True, stack_info=True)
    data = {"detail": "Internal Server Error."}
    tb_str = traceback.format_exc()
    is_staff = getattr(request, "user", None) and request.user.is_staff
    encoded_payload = base64.b64encode(request.body).decode("utf-8") if request.body else None
    metadata = {
        "headers": obfuscate(dict(request.headers)),
        "method": request.method,
        "path": request.path,
        "GET": obfuscate(request.GET.dict()),
        "POST": obfuscate(request.POST.dict() if request.method == "POST" else {}),
        # note: we can do request.user because we set the user in the auth flow
        # otherwise it should be await request.auser()
        "user": str(request.user) if getattr(request, "user", None) else None,
    }
    if request.method in ("POST", "PUT", "PATCH") and request.headers.get("Content-Type") == "application/json":
        try:
            json_payload = obfuscate(orjson.loads(request.body))
            encoded_payload = None  # No reason to store an encoded payload if we have the json already
        except Exception:  # pragma: no cover
            json_payload = None
    else:
        json_payload = None
    if settings.DEBUG or is_staff:  # pragma: no cover
        data["traceback"] = tb_str
    path = f"{request.method} {request.path}"
    track_internal_error.delay(
        path=path,
        traceback_str=tb_str,
        encoded_payload=encoded_payload,
        json_payload=json_payload,
        metadata=metadata,
    )
    return Response(status=500, data=data)


def handle_django_validation_error(request: HttpRequest, exc: ValidationError | t.Type[ValidationError]) -> Response:
    """Handle a validation error.

    Args:
        request: The incoming HTTP request.
        exc: The exception.
    """
    logger.error("VALIDATION_ERROR", exc_info=True, stack_info=True)
    error_dict = {k: [ee for e in v for ee in e] for k, v in exc.error_dict.items()}
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
    return Response(status=400, data={"detail": "You submitted answers refer to a different questionnaire."})


def handle_missing_mandatory_answers_submission_error(
    request: HttpRequest, exc: MissingMandatoryAnswerError | t.Type[MissingMandatoryAnswerError]
) -> Response:
    """Handle a cross-questionnaire submission error."""
    return Response(status=400, data={"detail": "You are missing mandatory answers."})


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
    return Response(status=400, data={"detail": "You have created too many items."})


def handle_already_member_error(request: HttpRequest, exc: AlreadyMemberError | t.Type[AlreadyMemberError]) -> Response:
    """Handle an already member error."""
    return Response(status=400, data={"detail": "You are already a member of this organization."})


def handle_pending_membership_request_exists_error(
    request: HttpRequest, exc: PendingMembershipRequestExistsError | t.Type[PendingMembershipRequestExistsError]
) -> Response:
    """Handle a pending membership request exists error."""
    return Response(status=400, data={"detail": "You have a pending membership request for this organization."})


SENSITIVE_KEYS = {"password", "token", "x-api-key", "authorization", "authentication"}


def obfuscate(data: dict[str, t.Any]) -> dict[str, t.Any]:
    """Obfuscate sensitive data in payloads and headers."""
    new_data = deepcopy(data)
    for key in data.keys():
        if key.lower() in SENSITIVE_KEYS:
            new_data[key] = "********"
    return new_data
