"""Cloudflare Turnstile verification for anonymous registration.

Fails CLOSED on a missing/rejected token (and on any 4xx, so a malformed request can never bypass
the check) and fails OPEN when Cloudflare is unreachable, 5xx, or reports ``internal-error``: an
outage isn't attacker-controlled, and blocking every real signup during one is the worse failure.
Operator-side problems (wrong secret, 4xx) are logged at error so they don't pass as a bot wave.
"""

import httpx
import structlog
from django.conf import settings
from django.utils.translation import gettext as _

from accounts.exceptions import TurnstileFailedError

logger = structlog.get_logger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# Bound connect separately so a slow Cloudflare can't pin a worker for long per registration.
_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
# error-codes caused by our configuration rather than by the visitor.
_OPERATOR_ERRORS = frozenset({"missing-input-secret", "invalid-input-secret"})


def is_turnstile_enabled() -> bool:
    """Return True only when both the site key and the secret key are configured."""
    return bool(settings.TURNSTILE_SITE_KEY and settings.TURNSTILE_SECRET_KEY)


def verify_turnstile(token: str | None, remote_ip: str) -> None:
    """Verify a Turnstile token with Cloudflare.

    Args:
        token: The ``cf-turnstile-response`` value posted by the client.
        remote_ip: The visitor IP (sent as Cloudflare's optional ``remoteip`` signal when known).

    Raises:
        TurnstileFailedError: The token is missing or Cloudflare rejected it.
    """
    if not is_turnstile_enabled():
        return
    message = str(_("Bot verification failed. Please try again."))
    if not token or not token.strip():
        logger.info("turnstile_failed", reason="missing_token")
        raise TurnstileFailedError(message)

    data = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        response = httpx.post(SITEVERIFY_URL, data=data, timeout=_TIMEOUT)
        result = response.json() if response.is_success else {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("turnstile_unavailable", error=str(exc))
        return
    if response.is_server_error or not isinstance(result, dict):
        logger.warning("turnstile_unavailable", status_code=response.status_code)
        return
    if result.get("success") is True:
        return

    error_codes = result.get("error-codes", [])
    if not isinstance(error_codes, list):
        logger.warning("turnstile_unavailable", error="malformed error-codes")
        return
    if "internal-error" in error_codes:
        logger.warning("turnstile_unavailable", error="internal-error")
        return
    if not response.is_success or _OPERATOR_ERRORS.intersection(error_codes):
        # A 4xx or a secret problem is ours, not the visitor's: reject (never bypass) but make it loud.
        logger.error("turnstile_misconfigured", status_code=response.status_code, error_codes=error_codes)
    else:
        logger.info("turnstile_failed", reason="rejected", error_codes=error_codes)
    raise TurnstileFailedError(message)
