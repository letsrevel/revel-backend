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
    DuplicateDiscountCodeError,
    InvalidPeriodError,
    InvalidResourceStateError,
    InvalidStripeWebhookSignatureError,
    InvalidZoneSelectionError,
    MembershipPolicyManageSubscriptionsOnlyError,
    OrganizationTokenGrantInvariantError,
    OrganizationTokenMembershipTierRequiredError,
    OrganizationTokenStaffGrantForbidden,
    PendingMembershipRequestExistsError,
    RevenueReportCadenceOwnerOnlyError,
    SeriesPassCoverageError,
    SeriesPassHasHoldersError,
    SeriesPassNotPurchasableError,
    SessionTotalMismatchError,
    StripeNotConnectedError,
    TicketAlreadyCancelledError,
    TooManyItemsError,
)
from events.service.event_manager import UserIsIneligibleError
from events.service.organization_service import (
    GRANT_INVARIANT_MESSAGE,
    MEMBERSHIP_POLICY_MANAGE_SUBSCRIPTIONS_MESSAGE,
    MEMBERSHIP_TIER_REQUIRED_MESSAGE,
    REVENUE_CADENCE_OWNER_ONLY_MESSAGE,
    STAFF_GRANT_FORBIDDEN_MESSAGE,
)
from events.service.ticket_service import (
    BILLING_INFO_REQUIRED_MESSAGE,
    STRIPE_NOT_CONNECTED_MESSAGE,
    TICKET_ALREADY_CANCELLED_MESSAGE,
)


def handle_user_is_ineligible_error(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Handle a user is-ineligible error by returning the eligibility payload.

    Args:
        request: The current HTTP request (unused; required by the handler signature).
        exc: The raised ``UserIsIneligibleError`` carrying the eligibility report.

    Returns:
        Response: A 400 response whose body is the serialized eligibility payload.
    """
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
    # revenue_report_cadence is owner-only (financially sensitive); staff-grantable
    # edit_organization must not reach it → 403.
    RevenueReportCadenceOwnerOnlyError: make_static_handler(403, REVENUE_CADENCE_OWNER_ONLY_MESSAGE),
    # membership subscription-policy fields belong to the manage_subscriptions domain;
    # staff with only edit_organization must not change them via the org-edit endpoint → 403.
    MembershipPolicyManageSubscriptionsOnlyError: make_static_handler(
        403, MEMBERSHIP_POLICY_MANAGE_SUBSCRIPTIONS_MESSAGE
    ),
    # Ticket/tier guards (formerly mapped via try/except in the tickets controller).
    TicketAlreadyCancelledError: make_static_handler(409, TICKET_ALREADY_CANCELLED_MESSAGE),
    StripeNotConnectedError: make_static_handler(400, STRIPE_NOT_CONNECTED_MESSAGE),
    BillingInfoRequiredError: make_static_handler(422, BILLING_INFO_REQUIRED_MESSAGE),
    # Stripe webhook signature failures answer 403 — fail closed, no detail leak.
    InvalidStripeWebhookSignatureError: make_static_handler(403, _("Invalid Stripe signature")),
    # Duplicate discount code → 409 with a clear, translatable message instead of an opaque 500.
    DuplicateDiscountCodeError: make_static_handler(409, _("A discount code with this code already exists.")),
    # Best-available zone selection: a missing/unknown/foreign price category is bad
    # buyer input, and the message names the tier's sellable zones → 400.
    InvalidZoneSelectionError: make_simple_handler(400),
    # Mutually exclusive period selectors (month + quarter together) → 422.
    InvalidPeriodError: make_simple_handler(422),
    # Series pass enable-time coverage gate — bad input, so 400.
    SeriesPassCoverageError: make_simple_handler(400),
    # Series pass exists but can't be purchased right now (sold out, sales window, etc.) → 409.
    SeriesPassNotPurchasableError: make_simple_handler(409),
    # Deleting a pass / removing tier-link coverage would strand a non-cancelled holder → 409.
    SeriesPassHasHoldersError: make_simple_handler(409),
    # Books-vs-charge invariant breach (#739): a bug on our side, and one we must never
    # paper over — 500, with a generic message so the amounts stay in the logs only.
    SessionTotalMismatchError: make_static_handler(500, _("Payment processing failed. Please try again later.")),
}


def register() -> None:
    """Install events exception handlers on the global Ninja API.

    Called from :meth:`events.apps.EventsConfig.ready`. Imports the global
    ``api`` lazily to avoid AppConfig import-cycle issues.
    """
    from api.api import api

    register_handlers(api, HANDLERS)
