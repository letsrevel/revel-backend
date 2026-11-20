"""Authentication service layer."""

from datetime import datetime, timedelta
from uuid import uuid4

import structlog
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_jwt.schema import TokenObtainPairOutputSchema
from ninja_jwt.tokens import RefreshToken

from accounts import schema
from accounts.exceptions import EmailNotVerifiedError
from accounts.jwt import TOTPJWTPayload, check_blacklist, create_token, validate_otp_jwt
from accounts.jwt import blacklist as blacklist_token
from accounts.models import RevelUser
from accounts.service import totp as totp_service
from accounts.service.tenant_session import tenant_claims
from common.keys import signing_key

logger = structlog.get_logger(__name__)


def get_temporary_otp_jwt(user: RevelUser) -> str:
    """Get a temporary JWT.

    This will need to be used by a user in combination with their OTP code to obtain a valid JWT.
    """
    logger.info("otp_jwt_generated", user_id=str(user.id), email=user.email)
    payload = TOTPJWTPayload(
        sub=str(user.id),
        aud=settings.JWT_AUDIENCE,
        email=user.email,
        user_id=user.id,
        iat=datetime.now(),
        exp=datetime.now() + timedelta(minutes=5),
        jti=uuid4().hex,
    )
    return create_token(payload.model_dump(mode="json"), signing_key(), settings.JWT_ALGORITHM)


def verify_otp_jwt(token: str, otp: str) -> tuple[RevelUser, bool]:
    """Verify the OTP JWT token and the TOTP code.

    Raises:
        TOTPLockedError: Too many wrong codes for this account (see ``accounts.service.totp``).
    """
    user = _burn_temporary_token(token)
    is_valid = totp_service.verify_totp(user, otp, reason="login")
    logging_method, result = (
        (logger.info, "otp_verification_success") if is_valid else (logger.warning, "otp_verification_failure")
    )
    logging_method(result, user_id=str(user.id), email=user.email)
    return user, is_valid


# The atomic block must end *before* the TOTP check: raising ``TOTPLockedError`` out of it would roll
# back its savepoint (under ``ATOMIC_REQUESTS`` the 429 response itself still commits), un-burning the
# temp token and discarding the ``on_commit`` lock email registered by ``verify_totp``.
@transaction.atomic
def _burn_temporary_token(token: str) -> RevelUser:
    """Validate the temporary token, blacklist it (single use) and return its user."""
    validated_token = validate_otp_jwt(token)
    check_blacklist(validated_token.jti)
    if validated_token.type != "totp-access":
        logger.warning("otp_verification_invalid_token_type", token_type=validated_token.type)  # type: ignore[unreachable]
        raise HttpError(401, str(_("Invalid token type.")))
    blacklist_token(token)
    return get_object_or_404(RevelUser, id=validated_token.user_id)


def is_session_allowed(user: RevelUser) -> bool:
    """Whether the account may hold a session: guests always, everyone else only with a verified email."""
    return user.guest or user.email_verified


def require_verified_email(user: RevelUser) -> None:
    """Refuse a login for a non-guest account whose email nobody has verified.

    Call it once the password has checked out and before any 2FA step (``/auth/token/pair`` calls it
    ahead of the TOTP branch), so the refusal never tells a caller anything about an account they could
    not already prove the password for.

    Raises:
        EmailNotVerifiedError: The account's email is not verified (403, code ``email_not_verified``).
    """
    if not is_session_allowed(user):
        logger.info("login_refused_email_not_verified", user_id=str(user.id))
        raise EmailNotVerifiedError(str(_("Please verify your email address before signing in.")))


def get_token_pair_for_user(user: RevelUser, *, tenant_host: str | None = None) -> TokenObtainPairOutputSchema:
    """Get a token pair for the user.

    Args:
        user: The user to issue tokens for.
        tenant_host: The active tenant host the login happened on, if any. Binds the session to it
            (see ``accounts.service.tenant_session``). Only the interactive login paths pass it;
            OIDC, SSO and impersonation stay platform-only.
    """
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])

    logger.info("token_pair_generated", user_id=str(user.id), email=user.email)
    token = RefreshToken.for_user(user)
    token.payload.update(schema.RevelUserSchema.from_orm(user).model_dump(mode="json"))
    token.payload.update(
        {
            "sub": str(user.id),
            "groups": list(user.groups.values_list("name", flat=True)),
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
        }
    )
    if tenant_host:
        token.payload.update(tenant_claims(tenant_host))
    return TokenObtainPairOutputSchema(
        username=user.username,
        access=str(token.access_token),  # type: ignore[attr-defined]
        refresh=str(token),
    )
