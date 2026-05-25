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

from common.exception_handlers import (
    ExceptionHandler,
    make_simple_handler,
    make_static_handler,
    register_handlers,
)
from events.exceptions import (
    AlreadyMemberError,
    BillingInfoRequiredError,
    InvalidResourceStateError,
    OrganizationTokenGrantInvariantError,
    OrganizationTokenMembershipTierRequiredError,
    OrganizationTokenStaffGrantForbidden,
    PendingMembershipRequestExistsError,
    StripeNotConnectedError,
    TicketAlreadyCancelledError,
    TooManyItemsError,
)
from events.service.event_manager import UserIsIneligibleError
from events.service.organization_service import (
    GRANT_INVARIANT_MESSAGE,
    MEMBERSHIP_TIER_REQUIRED_MESSAGE,
    STAFF_GRANT_FORBIDDEN_MESSAGE,
)
from events.service.ticket_service import (
    BILLING_INFO_REQUIRED_MESSAGE,
    STRIPE_NOT_CONNECTED_MESSAGE,
    TICKET_ALREADY_CANCELLED_MESSAGE,
)


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
    # EventResource.clean() invariant. NOTE: raised inside the model ``clean()``,
    # so ``Model.full_clean()`` re-wraps it as a *generic* Django ``ValidationError``
    # (subclass identity lost) and the global ``ValidationError`` handler answers
    # 400 ``{errors}`` instead. Registered here for future-proofing — it only wins
    # if the error is ever raised directly outside ``full_clean``. It carries a
    # ``ValidationError`` ``message_dict``, so render that content.
    InvalidResourceStateError: make_simple_handler(422),
    # Organization-token guards (formerly mapped via try/except in the tokens controller).
    OrganizationTokenStaffGrantForbidden: make_static_handler(403, STAFF_GRANT_FORBIDDEN_MESSAGE),
    OrganizationTokenGrantInvariantError: make_static_handler(422, GRANT_INVARIANT_MESSAGE),
    OrganizationTokenMembershipTierRequiredError: make_static_handler(422, MEMBERSHIP_TIER_REQUIRED_MESSAGE),
    # Ticket/tier guards (formerly mapped via try/except in the tickets controller).
    TicketAlreadyCancelledError: make_static_handler(409, TICKET_ALREADY_CANCELLED_MESSAGE),
    StripeNotConnectedError: make_static_handler(400, STRIPE_NOT_CONNECTED_MESSAGE),
    BillingInfoRequiredError: make_static_handler(422, BILLING_INFO_REQUIRED_MESSAGE),
}


def register() -> None:
    """Install events exception handlers on the global Ninja API.

    Called from :meth:`events.apps.EventsConfig.ready`. Imports the global
    ``api`` lazily to avoid AppConfig import-cycle issues.
    """
    from api.api import api

    register_handlers(api, HANDLERS)
