"""Wallet exception handlers.

Registered on the global ``NinjaExtraAPI`` from
:meth:`wallet.apps.WalletConfig.ready`. Signing and generation failures are
infrastructure faults (an unreadable certificate, a service-account key the
container cannot open), not caller mistakes, so they answer 503 — the status
the wallet routes already declare for "this rail is unavailable" — instead of
falling through to the generic 500 handler.

``ApplePassGeneratorError`` also wraps genuinely unexpected failures inside the
generator, so mapping it here trades a loud 500 for a "try again later". The
error log below keeps those diagnosable: it is the same signal that made the
container certificate-permissions incident traceable in Loki.

The reusable handler factories and the registration loop live in
:mod:`common.exception_handlers`. A static message is deliberate — ``str(exc)``
carries filesystem paths (``Certificate not found: /app/certs/pass.pem``).
"""

import typing as t

import structlog
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja.responses import Response

from common.exception_handlers import ExceptionHandler, register_handlers
from wallet.apple.generator import ApplePassGeneratorError
from wallet.apple.signer import ApplePassSignerError
from wallet.google.signer import GooglePassSignerError
from wallet.schema import WalletPassErrorCode, WalletPassErrorSchema

logger = structlog.get_logger(__name__)

PASS_UNAVAILABLE_MESSAGE = _("Wallet pass generation is temporarily unavailable. Please try again later.")


def handle_pass_unavailable(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Render a wallet signer/generator failure as a 503, and log it with its traceback.

    The body carries a stable ``code``: the same 503 status is already used for
    the un-``code``d "Wallet is not configured" refusal, and the frontend cannot
    key on the translated ``detail`` to tell the two apart (#905 precedent).

    Args:
        request: The current HTTP request.
        exc: The raised wallet error (its message stays in the logs only).

    Returns:
        Response: A 503 response shaped like ``WalletPassErrorSchema``.
    """
    logger.error(
        "wallet_pass_unavailable",
        exc_info=exc,
        path=request.path,
        exception_type=type(exc).__name__,
        exception_message=str(exc),
    )
    return Response(
        status=503,
        data=WalletPassErrorSchema(
            detail=str(PASS_UNAVAILABLE_MESSAGE),
            code=WalletPassErrorCode.PASS_UNAVAILABLE,
        ).model_dump(mode="json"),
    )


# Single source of truth for the exception → status mapping.
HANDLERS: dict[type[Exception], ExceptionHandler] = {
    ApplePassSignerError: handle_pass_unavailable,
    ApplePassGeneratorError: handle_pass_unavailable,
    GooglePassSignerError: handle_pass_unavailable,
}


def register() -> None:
    """Install wallet exception handlers on the global Ninja API.

    Called from :meth:`wallet.apps.WalletConfig.ready`. Imports the global
    ``api`` lazily to avoid AppConfig import-cycle issues.
    """
    from api.api import api

    register_handlers(api, HANDLERS)
