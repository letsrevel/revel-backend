## src/accounts/tests/test_account_service.py

from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.utils import timezone
from ninja.errors import HttpError

from accounts import schema
from accounts.jwt import create_token
from accounts.models import RevelUser
from accounts.service import account as account_service

pytestmark = pytest.mark.django_db


@patch("accounts.tasks.send_verification_email.delay")
def test_register_user_success(mock_send_email: MagicMock, valid_register_payload: schema.RegisterUserSchema) -> None:
    """Test successful user registration creates a user and sends an email."""
    assert RevelUser.objects.count() == 0

    user, token = account_service.register_user(valid_register_payload)

    assert RevelUser.objects.count() == 1
    assert user.username == valid_register_payload.email
    assert not user.email_verified
    assert user.is_active  # Active by default to allow email verification
    mock_send_email.assert_called_once_with(user.email, token)


@patch("accounts.tasks.send_verification_email.delay")
def test_register_user_already_exists(
    mock_send_email: MagicMock, user: RevelUser, valid_register_payload: schema.RegisterUserSchema
) -> None:
    """Test registering with an email that already exists raises an HttpError."""
    valid_register_payload.email = user.email
    user.email_verified = True  # Make user verified
    user.save()

    with pytest.raises(HttpError, match="A user with this email already exists."):
        account_service.register_user(valid_register_payload)

    assert RevelUser.objects.count() == 1
    mock_send_email.assert_not_called()


def test_verify_email_success(user: RevelUser) -> None:
    """Test that a valid verification token correctly verifies the user's email."""
    user.email_verified = False
    user.save()
    payload = schema.VerifyEmailJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    verified_user = account_service.verify_email(token)

    assert verified_user.id == user.id
    verified_user.refresh_from_db()
    assert verified_user.email_verified is True
    assert verified_user.is_active is True


def test_verify_email_invalid_token(user: RevelUser) -> None:
    """Test that verifying with a bad token raises an HttpError."""
    with pytest.raises(HttpError, match="Invalid token"):
        account_service.verify_email("this.is.a.bad.token")


@patch("accounts.tasks.send_password_reset_link.delay")
def test_request_password_reset_success(mock_send_email: MagicMock, user: RevelUser) -> None:
    """Test requesting a password reset sends an email for an existing user."""
    token = account_service.request_password_reset(user.email)
    assert token is not None
    mock_send_email.assert_called_once_with(user.email, token)


@patch("accounts.tasks.send_password_reset_link.delay")
def test_request_password_reset_nonexistent_user(mock_send_email: MagicMock) -> None:
    """Test that no email is sent for a nonexistent user (to prevent enumeration)."""
    token = account_service.request_password_reset("nobody@example.com")
    assert token is None
    mock_send_email.assert_not_called()


@patch("accounts.tasks.send_password_reset_link.delay")
def test_request_password_reset_for_google_user(mock_send_email: MagicMock, google_user: RevelUser) -> None:
    """Test that password reset is disabled for Google SSO users."""
    token = account_service.request_password_reset(google_user.email)
    assert token is None
    mock_send_email.assert_not_called()


def test_reset_password_success(user: RevelUser) -> None:
    """Test that a user's password can be successfully reset with a valid token."""
    old_password_hash = user.password
    payload = schema.PasswordResetJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    new_valid_password = "a-new-valid-Password-123!"
    reset_user = account_service.reset_password(token, new_valid_password)

    reset_user.refresh_from_db()
    assert reset_user.password != old_password_hash
    assert reset_user.check_password(new_valid_password)


def test_reset_password_for_google_user_fails(google_user: RevelUser) -> None:
    """Test that attempting to reset a password for a Google SSO user fails."""
    payload = schema.PasswordResetJWTPayloadSchema(
        user_id=google_user.id,
        email=google_user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    with pytest.raises(HttpError, match="Cannot reset password for Google SSO users."):
        account_service.reset_password(token, "any-password")


@patch("accounts.tasks.send_account_deletion_link.delay")
def test_request_account_deletion(mock_send_email: MagicMock, user: RevelUser) -> None:
    """Test that requesting account deletion sends the correct email task."""
    token = account_service.request_account_deletion(user)
    assert token is not None
    mock_send_email.assert_called_once_with(user.email, token)


def test_confirm_account_deletion_success(user: RevelUser) -> None:
    """Test that a valid deletion token successfully deletes the user."""
    payload = schema.DeleteAccountJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    user_id = user.id

    assert RevelUser.objects.filter(id=user_id).exists()
    account_service.confirm_account_deletion(token)
    # With CELERY_TASK_ALWAYS_EAGER=True, the task runs synchronously
    assert not RevelUser.objects.filter(id=user_id).exists()
