"""Google Wallet save-link signer.

Signs a "fat" JWT (class + object embedded) with the GCP service-account key
and returns the ``https://pay.google.com/gp/v/save/{jwt}`` link. There is no
``exp`` claim: save links are embedded in ticket emails and must never go
stale. See: https://developers.google.com/wallet/tickets/events/web
"""

import functools
import json
import time
import typing as t

import jwt
import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

SAVE_URL_BASE = "https://pay.google.com/gp/v/save/"


class GooglePassSignerError(Exception):
    """Raised when the Google Wallet signer is misconfigured."""


@functools.lru_cache(maxsize=2)
def _load_service_account(path: str) -> tuple[str, str]:
    """Load (client_email, private_key_pem) from a service-account JSON file.

    Cached by path (mirrors the Apple rail's cached pass generator) so each
    notification render doesn't re-read the key file from disk.

    Raises:
        GooglePassSignerError: If the file is missing, unreadable, or malformed.
    """
    try:
        with open(path) as f:
            data = json.load(f)
        return data["client_email"], data["private_key"]
    except (OSError, KeyError, ValueError) as e:
        logger.error("google_wallet_sa_key_load_failed", path=path, error=str(e))
        raise GooglePassSignerError(f"Cannot load Google Wallet service account key: {e}")


class GooglePassSigner:
    """Signs Google Wallet fat JWTs with the configured service-account key."""

    def __init__(self) -> None:
        """Load the service-account credentials.

        Raises:
            GooglePassSignerError: If the key path is unset or unreadable.
        """
        key_path = settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH
        if not key_path:
            raise GooglePassSignerError("GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH is not set")
        self.client_email, self._private_key = _load_service_account(key_path)

    def save_url(self, payload: dict[str, t.Any]) -> str:
        """Sign the payload and return the save link.

        Args:
            payload: ``{"eventTicketClasses": [...], "eventTicketObjects": [...]}``.

        Returns:
            The ``https://pay.google.com/gp/v/save/{jwt}`` URL.
        """
        from common.models import SiteSettings

        origins = list(dict.fromkeys([settings.BASE_URL, SiteSettings.get_solo().frontend_base_url]))
        claims: dict[str, t.Any] = {
            "iss": self.client_email,
            "aud": "google",
            "typ": "savetowallet",
            "iat": int(time.time()),
            "origins": origins,
            "payload": payload,
        }
        token = jwt.encode(claims, self._private_key, algorithm="RS256")
        return SAVE_URL_BASE + token
