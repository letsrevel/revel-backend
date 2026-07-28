"""Accounts exception handlers.

Registered on the global ``NinjaExtraAPI`` from
:meth:`accounts.apps.AccountsConfig.ready`. Each entry maps an accounts-specific
exception to its HTTP status code, keeping controllers free of try/except
boilerplate.

Ninja Extra dispatches exceptions by MRO — most specific handler wins — so these
app-specific handlers take precedence over the generic global handlers defined in
:mod:`api.api`. The reusable handler factories and the registration loop live in
:mod:`common.exception_handlers`.
"""

import typing as t

from django.http import HttpRequest
from django.utils.translation import gettext as _
from ninja.responses import Response

from accounts.exceptions import ReferralForfeitureConfirmationRequiredError
from common.exception_handlers import ExceptionHandler, register_handlers

#: Machine-readable discriminator the frontend keys its consequence screen off.
REFERRAL_FORFEITURE_CODE = "referral_forfeiture_confirmation_required"


def handle_referral_forfeiture_confirmation_required(
    request: HttpRequest, exc: Exception | t.Type[Exception]
) -> Response:
    """Render the referral-forfeiture consequences as a structured 409.

    The deletion token is deliberately *not* burned on this path (the guard runs
    before ``blacklist_token``), so the user can re-submit the same token with
    ``force=true`` once they have seen what they would lose.
    """
    summary = t.cast(ReferralForfeitureConfirmationRequiredError, exc).summary
    detail = _(
        "Deleting your account will permanently forfeit %(count)s unpaid referral "
        "payout(s) totalling %(total)s %(currency)s, of which %(failed)s %(currency)s "
        "comes from failed transfers. Contact support before deleting if you want "
        "these paid out, or re-submit with force=true to delete anyway."
    ) % {
        "count": summary.unpaid_count,
        "total": summary.unpaid_total,
        "failed": summary.failed_total,
        "currency": summary.currency,
    }
    return Response(
        status=409,
        data={
            "detail": detail,
            "code": REFERRAL_FORFEITURE_CODE,
            "unpaid_count": summary.unpaid_count,
            "unpaid_total": str(summary.unpaid_total),
            "failed_total": str(summary.failed_total),
            "currency": summary.currency,
        },
    )


# Single source of truth for the exception → status mapping.
HANDLERS: dict[type[Exception], ExceptionHandler] = {
    ReferralForfeitureConfirmationRequiredError: handle_referral_forfeiture_confirmation_required,
}


def register() -> None:
    """Install accounts exception handlers on the global Ninja API.

    Called from :meth:`accounts.apps.AccountsConfig.ready`. Imports the global
    ``api`` lazily to avoid AppConfig import-cycle issues.
    """
    from api.api import api

    register_handlers(api, HANDLERS)
