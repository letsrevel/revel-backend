"""The JWT module.

Read about JWT here: https://auth0.com/docs/secure/tokens/json-web-tokens/json-web-token-claims
"""

import typing as t
from datetime import datetime

import jwt
import structlog
from django.conf import settings
from django.db import transaction
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from ninja.errors import HttpError
from ninja_extra.exceptions import AuthenticationFailed
from ninja_jwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from ninja_jwt.utils import datetime_from_epoch
from pydantic import UUID4, BaseModel, ConfigDict, TypeAdapter, field_serializer

logger = structlog.get_logger(__file__)


class _BaseJWTPayload(BaseModel):
    """The base JWT payload."""

    model_config = ConfigDict(use_enum_values=True, populate_by_name=True)

    iss: str
    aud: str
    sub: str
    exp: datetime
    iat: datetime
    email: str

    @field_serializer("exp")
    def serialize_exp(self, value: datetime) -> int:
        return int(value.timestamp())

    @field_serializer("iat")
    def serialize_iat(self, value: datetime) -> int:
        return int(value.timestamp())


class TOTPJWTPayload(_BaseJWTPayload):
    iss: t.Literal["https://api.letsrevel.io/"] = "https://api.letsrevel.io/"
    jti: str
    user_id: UUID4
    type: t.Literal["totp-access"] = "totp-access"


def validate_otp_jwt(
    token: str,
    key: str | None = None,
    audience: str | None = None,
    algorithms: list[str] | None = None,
) -> TOTPJWTPayload:
    """Verify and parse the JWT token.

    Args:
        token (str): The JWT token.
        key (str): The secret key.
        audience (str): The audience.
        algorithms (list): The algorithms.

    Returns:
        JWTPayload: The decoded JWT payload.

    Raises:
        AuthenticationFailed: If token verification fails.
    """
    key = key or settings.SECRET_KEY
    audience = audience or settings.JWT_AUDIENCE
    algorithms = algorithms or [settings.JWT_ALGORITHM]
    try:
        decoded_jwt = jwt.decode(
            token,
            key=key,
            audience=audience,
            algorithms=algorithms,
        )
        return TypeAdapter(TOTPJWTPayload).validate_python(decoded_jwt)
    except ExpiredSignatureError as e:
        logger.debug("token_has_expired")
        raise AuthenticationFailed("Token has expired.") from e
    except InvalidTokenError as e:
        logger.debug("invalid_token")
        raise AuthenticationFailed("Invalid token.") from e
    except Exception as e:
        logger.debug("authentication_failed", exc_info=str(e))
        raise AuthenticationFailed("Authentication failed.") from e


def create_token(payload: dict[str, t.Any], secret: str, algorithm: str) -> str:
    """Helper function to create a JWT token.

    Args:
        payload (dict): The payload.
        secret (str): The secret key.
        algorithm (str): The algorithm.

    Returns:
        str: The JWT token.
    """
    return jwt.encode(payload, secret, algorithm=algorithm)


def check_blacklist(jti: str) -> None:
    """Checks if this token is present in the token blacklist.  Raises `HttpError` if so."""
    if BlacklistedToken.objects.filter(token__jti=jti).exists():
        raise HttpError(401, "Token is blacklisted.")


@transaction.atomic
def blacklist(
    token: str,
    key: str | None = None,
    audience: str | None = None,
    algorithms: list[str] | None = None,
) -> BlacklistedToken:
    """Ensures this token is included in the outstanding token list and adds it to the blacklist."""
    key = key or settings.SECRET_KEY  # Note: we are using the Django secret key here instead of the Auth0 secret
    audience = (
        audience or settings.JWT_AUDIENCE
    )  # Note: we are using the Django JWT audience here instead of the Auth0 audience
    algorithms = algorithms or [settings.JWT_ALGORITHM]
    payload = jwt.decode(token, key=key, audience=audience, algorithms=algorithms, verify=False)
    jti = payload["jti"]
    exp = payload["exp"]

    # Ensure outstanding token exists with given jti
    token_db, _ = OutstandingToken.objects.get_or_create(
        jti=jti,
        defaults={
            "token": token,
            "expires_at": datetime_from_epoch(exp),
        },
    )

    return BlacklistedToken.objects.get_or_create(token=token_db)[0]
