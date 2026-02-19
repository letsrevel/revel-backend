# src/accounts/tests/test_account_service.py

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


class TestRegisterUserSchemaEmailNormalization:
    """Tests for email normalization in RegisterUserSchema."""

    def test_uppercase_email_converted_to_lowercase(self) -> None:
        """Test that uppercase email is converted to lowercase."""
        payload = schema.RegisterUserSchema(
            email="TEST@EXAMPLE.COM",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            accept_toc_and_privacy=True,
        )
        assert payload.email == "test@example.com"

    def test_mixed_case_email_converted_to_lowercase(self) -> None:
        """Test that mixed case email is converted to lowercase."""
        payload = schema.RegisterUserSchema(
            email="Test.User@Example.COM",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            accept_toc_and_privacy=True,
        )
        assert payload.email == "test.user@example.com"

    def test_lowercase_email_stays_lowercase(self) -> None:
        """Test that already lowercase email remains unchanged."""
        payload = schema.RegisterUserSchema(
            email="user@example.com",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            accept_toc_and_privacy=True,
        )
        assert payload.email == "user@example.com"

    @patch("accounts.tasks.send_verification_email.delay")
    def test_registration_saves_lowercase_email(self, mock_send_email: MagicMock) -> None:
        """Test that user registration saves email as lowercase in database."""
        payload = schema.RegisterUserSchema(
            email="MixedCase@Example.COM",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            accept_toc_and_privacy=True,
        )
        user, _ = account_service.register_user(payload)

        assert user.email == "mixedcase@example.com"
        assert user.username == "mixedcase@example.com"


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


@patch("accounts.tasks.send_verification_email.delay")
def test_register_user_converts_guest_user(mock_send_email: MagicMock, guest_user: RevelUser) -> None:
    """Test that registering with a guest user's email converts them to a full user."""
    payload = schema.RegisterUserSchema(
        email=guest_user.email,
        password1="a-Strong-password-123!",
        password2="a-Strong-password-123!",
        first_name="Real",
        last_name="Person",
        accept_toc_and_privacy=True,
    )

    user, token = account_service.register_user(payload)

    assert RevelUser.objects.count() == 1  # No new user created
    assert user.id == guest_user.id
    assert user.guest is False
    assert user.email_verified is False
    assert user.first_name == "Real"
    assert user.last_name == "Person"
    assert user.check_password("a-Strong-password-123!")
    mock_send_email.assert_called_once_with(user.email, token)


@patch("accounts.tasks.send_verification_email.delay")
def test_register_user_converts_verified_guest_user(mock_send_email: MagicMock, guest_user: RevelUser) -> None:
    """Test that registration converts a guest even if they anomalously have email_verified=True."""
    guest_user.email_verified = True
    guest_user.save(update_fields=["email_verified"])

    payload = schema.RegisterUserSchema(
        email=guest_user.email,
        password1="a-Strong-password-123!",
        password2="a-Strong-password-123!",
        first_name="Real",
        last_name="Person",
        accept_toc_and_privacy=True,
    )

    user, token = account_service.register_user(payload)

    assert RevelUser.objects.count() == 1
    assert user.id == guest_user.id
    assert user.guest is False
    assert user.email_verified is False  # Must re-verify even if anomalously verified before
    mock_send_email.assert_called_once_with(user.email, token)


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


def test_verify_email_blocks_guest_user(guest_user: RevelUser) -> None:
    """Test that guest users cannot verify their email through the registration flow."""
    payload = schema.VerifyEmailJWTPayloadSchema(
        user_id=guest_user.id, email=guest_user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    with pytest.raises(HttpError, match="Invalid verification token."):
        account_service.verify_email(token)

    guest_user.refresh_from_db()
    assert guest_user.guest is True
    assert guest_user.email_verified is False
    assert guest_user.is_active is True  # is_active must not be toggled


def test_verify_email_consistently_rejects_guest_on_retry(guest_user: RevelUser) -> None:
    """Test that a guest user is rejected on every attempt, not just the first.

    The guest guard fires before blacklist_token, so the token is never consumed
    and the guard blocks on every retry.
    """
    payload = schema.VerifyEmailJWTPayloadSchema(
        user_id=guest_user.id, email=guest_user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    with pytest.raises(HttpError, match="Invalid verification token."):
        account_service.verify_email(token)

    with pytest.raises(HttpError, match="Invalid verification token."):
        account_service.verify_email(token)


@patch("accounts.tasks.send_verification_email.delay")
def test_verify_email_succeeds_for_converted_guest(mock_send_email: MagicMock, guest_user: RevelUser) -> None:
    """Test that a former guest who registered properly can verify their email.

    Exercises the path where a token was created for a user who was originally a
    guest but has since been converted via register_user. The verify_email call
    must succeed because user.guest is now False.
    """
    # Convert the guest via registration
    payload = schema.RegisterUserSchema(
        email=guest_user.email,
        password1="a-Strong-password-123!",
        password2="a-Strong-password-123!",
        first_name="Real",
        last_name="Person",
        accept_toc_and_privacy=True,
    )
    user, token = account_service.register_user(payload)
    assert user.guest is False
    assert user.email_verified is False

    # Verify using the token from registration
    verified_user = account_service.verify_email(token)

    assert verified_user.id == guest_user.id
    assert verified_user.email_verified is True
    assert verified_user.guest is False


@patch("accounts.service.account.send_verification_email_for_user")
def test_resend_verification_email_blocks_guest_user(mock_send: MagicMock, guest_user: RevelUser) -> None:
    """Test that verification emails are not sent to guest users."""
    account_service.resend_verification_email(guest_user.email)

    mock_send.assert_not_called()
    guest_user.refresh_from_db()
    assert guest_user.guest is True
    assert guest_user.email_verified is False


@patch("accounts.tasks.send_verification_email.delay")
def test_register_user_existing_unverified_non_guest(
    mock_send_email: MagicMock, user: RevelUser, valid_register_payload: schema.RegisterUserSchema
) -> None:
    """Test that re-registering with an existing unverified non-guest email resends verification and raises."""
    valid_register_payload.email = user.email
    user.email_verified = False
    user.save(update_fields=["email_verified"])

    with pytest.raises(HttpError, match="A user with this email already exists."):
        account_service.register_user(valid_register_payload)

    assert RevelUser.objects.count() == 1
    mock_send_email.assert_called_once()


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


@patch("accounts.tasks.send_password_reset_link.delay")
def test_request_password_reset_for_guest_user(mock_send_email: MagicMock, guest_user: RevelUser) -> None:
    """Test that guest users can request a password reset (converts them on reset)."""
    token = account_service.request_password_reset(guest_user.email)
    assert token is not None
    mock_send_email.assert_called_once_with(guest_user.email, token)


def test_reset_password_converts_guest_user(guest_user: RevelUser) -> None:
    """Test that resetting password for a guest user converts them to a full user."""
    payload = schema.PasswordResetJWTPayloadSchema(
        user_id=guest_user.id, email=guest_user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)

    reset_user = account_service.reset_password(token, "a-new-valid-Password-123!")

    reset_user.refresh_from_db()
    assert reset_user.guest is False
    assert reset_user.email_verified is True
    assert reset_user.check_password("a-new-valid-Password-123!")


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
