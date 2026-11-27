"""Signed link tokens of the account flows: create them, and decode and check them."""

import typing as t

import jwt
import structlog
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja import Schema

from accounts import schema
from accounts.exceptions import (
    LinkTokenError,
    LinkTokenErrorCode,
)
from accounts.jwt import create_token
from accounts.models import RevelUser
from common.keys import decode_jwt, signing_key, verification_keys
from common.testing import (
    TOKEN_TYPE_DELETION,
    TOKEN_TYPE_EMAIL_CHANGE,
    TOKEN_TYPE_PASSWORD_RESET,
    TOKEN_TYPE_VERIFICATION,
    store_test_token,
)

# The logger name of the module before the split, so the log names stay the same.
logger = structlog.get_logger("accounts.service.account")


def create_verification_token(user: RevelUser) -> str:
    """Create a verification token for a user without sending email.

    Args:
        user (RevelUser): The user to create a token for.

    Returns:
        str: The verification token.
    """
    verification_payload = schema.VerifyEmailJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
        iat=timezone.now(),
    )
    token = create_token(verification_payload.model_dump(mode="json"), signing_key(), settings.JWT_ALGORITHM)
    store_test_token(TOKEN_TYPE_VERIFICATION, token)
    return token


def create_password_reset_token(user: RevelUser) -> str:
    """Create a password reset token for a user without sending email.

    Args:
        user: The user to create a token for.

    Returns:
        The password reset token.
    """
    payload = schema.PasswordResetJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), signing_key(), settings.JWT_ALGORITHM)
    store_test_token(TOKEN_TYPE_PASSWORD_RESET, token)
    return token


def create_deletion_token(user: RevelUser) -> str:
    """Create an account deletion token for a user without sending email.

    Args:
        user (RevelUser): The user to create a token for.

    Returns:
        str: The deletion token.
    """
    payload = schema.DeleteAccountJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), signing_key(), settings.JWT_ALGORITHM)
    store_test_token(TOKEN_TYPE_DELETION, token)
    return token


def create_email_change_token(user: RevelUser, new_email: str) -> str:
    """Create a single-use email change token bound to the user and the proposed new email.

    Args:
        user: The user requesting the change.
        new_email: The proposed new email (must already be lowercased).

    Returns:
        The signed JWT token.
    """
    payload = schema.EmailChangeJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        new_email=new_email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
        iat=timezone.now(),
    )
    token = create_token(payload.model_dump(mode="json"), signing_key(), settings.JWT_ALGORITHM)
    store_test_token(TOKEN_TYPE_EMAIL_CHANGE, token)
    return token


T = t.TypeVar("T", bound=Schema)


def token_to_payload(token: str, schema_class: t.Type[T], keys: list[str] | None = None) -> T:
    """Decode a token and validate it against a schema.

    Args:
        token (str): The token to decode.
        schema_class (t.Type[Schema]): The schema to validate the token against.
        keys: Keys to verify the signature with (defaults to the current and fallback SECRET_KEYs).

    Returns:
        Schema: The decoded and validated token.
    """
    try:
        _payload = decode_jwt(
            token, keys or verification_keys(), algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
        # The schemas default ``type`` for construction; a decoded token must state exactly that type,
        # or a session token (same key and audience, no ``type`` claim) would inherit the purpose.
        type_field = schema_class.model_fields.get("type")
        if type_field is None or _payload.get("type") != type_field.default:
            raise ValueError("token type mismatch")
        return schema_class.model_validate(_payload)
    except jwt.ExpiredSignatureError:
        logger.warning("token_validation_expired", token_type=schema_class.__name__)
        raise LinkTokenError(LinkTokenErrorCode.TOKEN_EXPIRED, str(_("Token has expired.")))
    except Exception as e:
        logger.warning("token_validation_failed", token_type=schema_class.__name__, error=str(e))
        raise LinkTokenError(LinkTokenErrorCode.INVALID_TOKEN, str(_("Invalid token.")))
