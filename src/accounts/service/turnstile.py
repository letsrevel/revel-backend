"""Cloudflare Turnstile verification for anonymous registration.

Fails CLOSED on a missing/rejected token and fails OPEN when Cloudflare is unreachable: an outage
isn't attacker-controlled, and blocking every real signup during one is the worse failure.
"""

import httpx
import structlog
from django.conf import settings
from django.utils.translation import gettext as _

from accounts.exceptions import TurnstileFailedError

logger = structlog.get_logger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


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
        response = httpx.post(SITEVERIFY_URL, data=data, timeout=5)
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("turnstile_unavailable", error=str(exc))
        return

    if not result.get("success"):
        logger.info("turnstile_failed", reason="rejected", error_codes=result.get("error-codes", []))
        raise TurnstileFailedError(message)
