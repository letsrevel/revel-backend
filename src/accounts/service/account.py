"""Service layer for the authentication app."""

import typing as t

import jwt
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


def register_user(payload: schema.RegisterUserSchema) -> tuple[RevelUser, str]:
    """Register a new user and send a verification email.

    Args:
        payload (schema.RegisterUserSchema): The user data.

    Returns:
        RevelUser: The newly created user.
    """
    if existing_user := RevelUser.objects.filter(username=payload.email).first():
        if not existing_user.email_verified:  # pragma: no branch
            send_verification_email_for_user(existing_user)
        raise HttpError(400, str(_("A user with this email already exists.")))
    new_user = RevelUser.objects.create_user(
        username=payload.email,
        email=payload.email,
        password=payload.password1,
        first_name=payload.first_name,
        last_name=payload.last_name,
        is_active=True,  # we use email verification
    )
    return send_verification_email_for_user(new_user)


def send_verification_email_for_user(user: RevelUser) -> tuple[RevelUser, str]:
    """Send a verification email for a user."""
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
        return user
    raise HttpError(400, str(_("A user with this email no longer exists.")))


def request_password_reset(email: str) -> str | None:
    """Request a password reset.

    Args:
        email (str): The email address of the user.

    Returns:
        None
    """
    try:
        user = RevelUser.objects.get(username=email)
    except RevelUser.DoesNotExist:
        return None
    if GoogleSSOUser.objects.filter(user=user).exists():
        return None
    payload = schema.PasswordResetJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    tasks.send_password_reset_link.delay(user.email, token)
    return token


def request_account_deletion(user: RevelUser) -> str:
    """Request account deletion.

    Args:
        user (RevelUser): The user.

    Returns:
        str: The token.
    """
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
        raise HttpError(400, str(_("Cannot reset password for Google SSO users.")))
    validate_password(new_password, user=user)
    user.set_password(new_password)
    user.save()
    blacklist_token(token)
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
        raise HttpError(
            400,
            str(
                _(
                    "You cannot delete your account while you own organizations. "
                    "Please contact support to transfer ownership or delete the organizations first."
                )
            ),
        )

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
        raise HttpError(400, str(_("Token has expired.")))
    except Exception as e:
        raise HttpError(400, str(_("Invalid token: {error}")).format(error=e))
