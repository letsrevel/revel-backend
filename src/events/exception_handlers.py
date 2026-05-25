"""Events exception handlers.

Registered on the global ``NinjaExtraAPI`` from :meth:`events.apps.EventsConfig.ready`.
Each entry maps an events-specific exception to its HTTP status code, keeping
controllers free of try/except boilerplate.

Ninja Extra dispatches exceptions by MRO — most specific handler wins — so these
app-specific handlers take precedence over the generic ``ValidationError → 400``
global defined in :mod:`api.api`. The reusable handler factories and the
registration loop live in :mod:`common.exception_handlers`.
"""

import typing as t

from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja.responses import Response

from common.exception_handlers import ExceptionHandler, make_static_handler, register_handlers
from events.exceptions import (
    AlreadyMemberError,
    PendingMembershipRequestExistsError,
    TooManyItemsError,
)
from events.service.event_manager import UserIsIneligibleError


def handle_user_is_ineligible_error(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Handle a user is-ineligible error by returning the eligibility payload."""
    return Response(status=400, data=t.cast(UserIsIneligibleError, exc).eligibility.model_dump(mode="json"))


# Single source of truth for the exception → status mapping.
HANDLERS: dict[type[Exception], ExceptionHandler] = {
    UserIsIneligibleError: handle_user_is_ineligible_error,
    TooManyItemsError: make_static_handler(400, _("You have created too many items.")),
    AlreadyMemberError: make_static_handler(400, _("You are already a member of this organization.")),
    PendingMembershipRequestExistsError: make_static_handler(
        400, _("You have a pending membership request for this organization.")
    ),
}


def register() -> None:
    """Install events exception handlers on the global Ninja API.

    Called from :meth:`events.apps.EventsConfig.ready`. Imports the global
    ``api`` lazily to avoid AppConfig import-cycle issues.
    """
    from api.api import api

    register_handlers(api, HANDLERS)
