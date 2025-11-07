"""Service layer for the authentication app."""

import typing as t

import jwt
import structlog
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_google_sso.models import GoogleSSOUser
from ninja import Schema
from ninja.errors import HttpError

from accounts import schema, tasks
from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import check_blacklist, create_token
from accounts.models import RevelUser
from accounts.password_validation import validate_password

logger = structlog.get_logger(__name__)


def register_user(payload: schema.RegisterUserSchema) -> tuple[RevelUser, str]:
    """Register a new user and send a verification email.

    Args:
        payload (schema.RegisterUserSchema): The user data.

    Returns:
        RevelUser: The newly created user.
    """
    logger.info("user_registration_started", email=payload.email)
    if existing_user := RevelUser.objects.filter(username=payload.email).first():
        if not existing_user.email_verified:  # pragma: no branch
            logger.info("user_registration_duplicate_unverified", email=payload.email)
            send_verification_email_for_user(existing_user)
        logger.warning("user_registration_duplicate", email=payload.email)
        raise HttpError(400, str(_("A user with this email already exists.")))
    new_user = RevelUser.objects.create_user(
        username=payload.email,
        email=payload.email,
        password=payload.password1,
        first_name=payload.first_name,
        last_name=payload.last_name,
        is_active=True,  # we use email verification
    )
    logger.info("user_registration_completed", user_id=str(new_user.id), email=new_user.email)
    return send_verification_email_for_user(new_user)


def send_verification_email_for_user(user: RevelUser) -> tuple[RevelUser, str]:
    """Send a verification email for a user."""
    logger.info("verification_email_requested", user_id=str(user.id), email=user.email)
    verification_payload = schema.VerifyEmailJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    tasks.send_verification_email.delay(user.email, token)
    return user, token


@transaction.atomic
def verify_email(token: str) -> RevelUser:
    """Verify a user's email.

    Args:
        token (str): The verification token.

    Returns:
        RevelUser: The verified user.
    """
    payload = token_to_payload(token, schema.VerifyEmailJWTPayloadSchema)
    check_blacklist(payload.jti)
    if user := RevelUser.objects.filter(id=payload.user_id).first():
        blacklist_token(token)
        user.is_active = user.email_verified = True
        user.save()
        logger.info("email_verified", user_id=str(user.id), email=user.email)
        return user
    logger.warning("email_verification_failed_user_not_found", user_id=str(payload.user_id))
    raise HttpError(400, str(_("A user with this email no longer exists.")))


def request_password_reset(email: str) -> str | None:
    """Request a password reset.

    Args:
        email (str): The email address of the user.

    Returns:
        None
    """
    logger.info("password_reset_requested", email=email)
    try:
        user = RevelUser.objects.get(username=email)
    except RevelUser.DoesNotExist:
        logger.info("password_reset_user_not_found", email=email)
        return None
    if GoogleSSOUser.objects.filter(user=user).exists():
        logger.info("password_reset_blocked_google_sso", user_id=str(user.id), email=email)
        return None
    payload = schema.PasswordResetJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    tasks.send_password_reset_link.delay(user.email, token)
    logger.info("password_reset_email_sent", user_id=str(user.id), email=email)
    return token


def request_account_deletion(user: RevelUser) -> str:
    """Request account deletion.

    Args:
        user (RevelUser): The user.

    Returns:
        str: The token.
    """
    logger.info("account_deletion_requested", user_id=str(user.id), email=user.email)
    payload = schema.DeleteAccountJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    tasks.send_account_deletion_link.delay(user.email, token)
    return token


@transaction.atomic
def reset_password(token: str, new_password: str) -> RevelUser:
    """Reset a user's password.

    Args:
        token (str): The password reset token.
        new_password (str): The new password.
    """
    payload = token_to_payload(token, schema.PasswordResetJWTPayloadSchema)
    check_blacklist(payload.jti)
    user = get_object_or_404(RevelUser, id=payload.user_id)
    if GoogleSSOUser.objects.filter(user=user).exists():
        logger.warning("password_reset_blocked_google_sso_user", user_id=str(user.id), email=user.email)
        raise HttpError(400, str(_("Cannot reset password for Google SSO users.")))
    validate_password(new_password, user=user)
    user.set_password(new_password)

    # Convert guest user to full user when setting password
    if user.guest:
        user.guest = False
        user.email_verified = True
        user.save(update_fields=["password", "guest", "email_verified"])
        logger.info("guest_user_converted_to_full_user", user_id=str(user.id), email=user.email)
    else:
        user.save(update_fields=["password"])

    blacklist_token(token)
    logger.info("password_reset_completed", user_id=str(user.id), email=user.email)
    return user


@transaction.atomic
def confirm_account_deletion(token: str) -> None:
    """Confirm and execute account deletion.

    This function validates the deletion token and enqueues a background task
    to delete the user account. The actual deletion is handled asynchronously
    via Celery to avoid blocking the request for users with many relationships.

    Args:
        token (str): The account deletion token.

    Raises:
        HttpError: If the user owns organizations (requires manual intervention).
    """
    payload = token_to_payload(token, schema.DeleteAccountJWTPayloadSchema)
    check_blacklist(payload.jti)
    user = get_object_or_404(RevelUser, id=payload.user_id)

    # Check if user owns any organizations - this would block deletion
    if user.owned_organizations.exists():
        org_count = user.owned_organizations.count()
        logger.warning(
            "account_deletion_blocked_owns_organizations", user_id=str(user.id), email=user.email, org_count=org_count
        )
        raise HttpError(
            400,
            str(
                _(
                    "You cannot delete your account while you own organizations. "
                    "Please contact support to transfer ownership or delete the organizations first."
                )
            ),
        )

    logger.info("account_deletion_confirmed", user_id=str(user.id), email=user.email)
    blacklist_token(token)
    # Enqueue the deletion as a background task to handle heavy operations
    tasks.delete_user_account.delay(str(user.id))


T = t.TypeVar("T", bound=Schema)


def token_to_payload(token: str, schema_class: t.Type[T]) -> T:
    """Decode a token and validate it against a schema.

    Args:
        token (str): The token to decode.
        schema_class (t.Type[Schema]): The schema to validate the token against.

    Returns:
        Schema: The decoded and validated token.
    """
    try:
        _payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
        return schema_class.model_validate(_payload)
    except jwt.ExpiredSignatureError:
        logger.warning("token_validation_expired", token_type=schema_class.__name__)
        raise HttpError(400, str(_("Token has expired.")))
    except Exception as e:
        logger.warning("token_validation_failed", token_type=schema_class.__name__, error=str(e))
        raise HttpError(400, str(_("Invalid token: {error}")).format(error=e))
