"""Signed, short-lived OAuth ``state`` for the org-level connect flow (spec §6).

Same signing primitives as the OIDC login token (``accounts.jwt``), a distinct ``type`` so
the two can never be swapped, and bound to organization + user so the callback can
re-verify ownership (closes the org-level login-CSRF variant).
"""

import secrets
import typing as t
from uuid import UUID

import jwt
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from pydantic import BaseModel, ValidationError

from accounts.jwt import create_token
from integrations.exceptions import IntegrationError
from integrations.schema import IntegrationErrorCode

CONNECT_STATE_COOKIE = "integrations_connect_state"
CONNECT_STATE_COOKIE_PATH = "/api/integrations"
_TOKEN_TYPE = "integrations-connect"


class ConnectStatePayload(BaseModel):
    organization_id: UUID
    user_id: UUID
    provider: str
    jti: str


def mint_state(*, organization_id: UUID, user_id: UUID, provider: str) -> str:
    """Create the state carried through the provider's consent screen."""
    now = timezone.now()
    payload: dict[str, t.Any] = {
        "iss": "https://api.letsrevel.io/",
        "aud": settings.JWT_AUDIENCE,
        "jti": secrets.token_urlsafe(16),
        "iat": int(now.timestamp()),
        "exp": int((now + settings.INTEGRATIONS_CONNECT_STATE_TTL).timestamp()),
        "type": _TOKEN_TYPE,
        "organization_id": str(organization_id),
        "user_id": str(user_id),
        "provider": provider,
    }
    return create_token(payload, settings.SECRET_KEY, settings.JWT_ALGORITHM)


def validate_state(state: str) -> ConnectStatePayload:
    """Verify signature, expiry and type; return the bound identifiers."""
    try:
        decoded = jwt.decode(
            state, key=settings.SECRET_KEY, audience=settings.JWT_AUDIENCE, algorithms=[settings.JWT_ALGORITHM]
        )
        if decoded.get("type") != _TOKEN_TYPE:
            raise jwt.InvalidTokenError("wrong type")
        return ConnectStatePayload.model_validate(decoded)
    except (jwt.PyJWTError, ValidationError) as e:
        raise IntegrationError(
            IntegrationErrorCode.STATE_INVALID,
            str(_("The connection request is invalid or has expired. Please try again.")),
        ) from e


def state_matches_cookie(cookie_value: str | None, state: str) -> bool:
    """Constant-time comparison of the callback ``state`` with the browser-bound cookie."""
    return cookie_value is not None and state.isascii() and secrets.compare_digest(cookie_value, state)
