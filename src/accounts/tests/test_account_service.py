# src/accounts/tests/test_account_service.py

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.utils import timezone
from ninja.errors import HttpError

from accounts import schema
from accounts.jwt import create_token
from accounts.models import Referral, ReferralCode, RevelUser
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


@patch("accounts.tasks.send_account_activation_link.delay")
def test_register_user_sends_activation_for_guest(mock_activation: MagicMock, guest_user: RevelUser) -> None:
    """Test that registering with a guest email sends activation email and raises 400."""
    original_password = guest_user.password
    payload = schema.RegisterUserSchema(
        email=guest_user.email,
        password1="a-Strong-password-123!",
        password2="a-Strong-password-123!",
        first_name="Real",
        last_name="Person",
        accept_toc_and_privacy=True,
    )

    with pytest.raises(HttpError, match="A user with this email already exists."):
        account_service.register_user(payload)

    mock_activation.assert_called_once()
    # Guest account must NOT be modified
    guest_user.refresh_from_db()
    assert guest_user.guest is True
    assert guest_user.first_name == "Guest"
    assert guest_user.last_name == "User"
    assert guest_user.password == original_password


@patch("accounts.tasks.send_account_activation_link.delay")
def test_register_user_sends_activation_for_verified_guest(mock_activation: MagicMock, guest_user: RevelUser) -> None:
    """Test that registration sends activation for a guest even with anomalous email_verified=True."""
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

    with pytest.raises(HttpError, match="A user with this email already exists."):
        account_service.register_user(payload)

    mock_activation.assert_called_once()
    guest_user.refresh_from_db()
    assert guest_user.guest is True


@patch("accounts.tasks.send_account_activation_link.delay")
def test_register_guest_activation_uses_password_reset_token(mock_activation: MagicMock, guest_user: RevelUser) -> None:
    """Test that the activation email uses a password-reset token so reset_password() handles conversion."""
    payload = schema.RegisterUserSchema(
        email=guest_user.email,
        password1="a-Strong-password-123!",
        password2="a-Strong-password-123!",
        accept_toc_and_privacy=True,
    )

    with pytest.raises(HttpError):
        account_service.register_user(payload)

    mock_activation.assert_called_once()
    token = mock_activation.call_args[0][1]
    # The token must be decodable as a PasswordResetJWTPayloadSchema
    decoded = account_service.token_to_payload(token, schema.PasswordResetJWTPayloadSchema)
    assert decoded.user_id == guest_user.id


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


def test_verify_email_succeeds_for_converted_guest(guest_user: RevelUser) -> None:
    """Test that a former guest who set their password can verify their email.

    Exercises the path where a guest was converted via reset_password() (the
    activation link flow). After conversion, the user is no longer a guest and
    email verification succeeds.
    """
    # Convert the guest via password reset (activation link flow)
    reset_payload = schema.PasswordResetJWTPayloadSchema(
        user_id=guest_user.id, email=guest_user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    reset_token = create_token(reset_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    account_service.reset_password(reset_token, "a-Strong-password-123!")

    guest_user.refresh_from_db()
    assert guest_user.guest is False
    assert guest_user.email_verified is True


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


@patch("accounts.tasks.send_account_activation_link.delay")
def test_full_guest_to_full_user_lifecycle(mock_activation: MagicMock, guest_user: RevelUser) -> None:
    """Test the complete guest activation lifecycle.

    1. Guest user exists with event data
    2. Someone registers with that email → 400, activation email sent
    3. Click activation link (password reset) → guest converted to full user
    """
    guest_id = guest_user.id

    # Step 1: Attempt registration — should get 400 + activation email
    payload = schema.RegisterUserSchema(
        email=guest_user.email,
        password1="a-Strong-password-123!",
        password2="a-Strong-password-123!",
        first_name="Real",
        last_name="Person",
        accept_toc_and_privacy=True,
    )

    with pytest.raises(HttpError, match="A user with this email already exists."):
        account_service.register_user(payload)

    mock_activation.assert_called_once()
    activation_token = mock_activation.call_args[0][1]

    # Guest is still a guest
    guest_user.refresh_from_db()
    assert guest_user.guest is True

    # Step 2: Click the activation link (password reset with the token)
    converted_user = account_service.reset_password(activation_token, "a-Strong-password-123!")

    # User is now a full user
    converted_user.refresh_from_db()
    assert converted_user.id == guest_id
    assert converted_user.guest is False
    assert converted_user.email_verified is True
    assert converted_user.check_password("a-Strong-password-123!")
    assert RevelUser.objects.count() == 1


class TestRegisterWithReferralCode:
    """Tests for referral code handling during registration."""

    @pytest.fixture
    def referrer(self, django_user_model: t.Type[RevelUser]) -> RevelUser:
        return django_user_model.objects.create_user(
            username="referrer@example.com",
            email="referrer@example.com",
            password="strong-password-123!",
        )

    @pytest.fixture
    def active_code(self, referrer: RevelUser) -> ReferralCode:
        return ReferralCode.objects.create(user=referrer, code="TESTCODE")

    @patch("accounts.tasks.send_verification_email.delay")
    def test_register_with_valid_referral_code(self, mock_send_email: MagicMock, active_code: ReferralCode) -> None:
        """Test that a valid referral code creates a Referral record."""
        payload = schema.RegisterUserSchema(
            email="referred@example.com",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            referral_code="TESTCODE",
            accept_toc_and_privacy=True,
        )

        user, _ = account_service.register_user(payload)

        referral = Referral.objects.get(referred_user=user)
        assert referral.referral_code == active_code
        assert referral.referrer == active_code.user
        assert referral.revenue_share_percent == settings.DEFAULT_REFERRAL_SHARE_PERCENT

    @patch("accounts.tasks.send_verification_email.delay")
    def test_register_with_case_insensitive_referral_code(
        self, mock_send_email: MagicMock, active_code: ReferralCode
    ) -> None:
        """Test that referral code lookup is case-insensitive."""
        payload = schema.RegisterUserSchema(
            email="referred@example.com",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            referral_code="testcode",
            accept_toc_and_privacy=True,
        )

        user, _ = account_service.register_user(payload)

        assert Referral.objects.filter(referred_user=user, referral_code=active_code).exists()

    def test_register_with_invalid_referral_code_fails(self) -> None:
        """Test that an invalid referral code raises 422."""
        payload = schema.RegisterUserSchema(
            email="referred@example.com",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            referral_code="NOSUCHCODE",
            accept_toc_and_privacy=True,
        )

        with pytest.raises(HttpError, match="Invalid or inactive referral code."):
            account_service.register_user(payload)

        assert RevelUser.objects.filter(email="referred@example.com").count() == 0
        assert Referral.objects.count() == 0

    def test_register_with_inactive_referral_code_fails(self, referrer: RevelUser) -> None:
        """Test that an inactive referral code raises 422."""
        ReferralCode.objects.create(user=referrer, code="DISABLED", is_active=False)

        payload = schema.RegisterUserSchema(
            email="referred@example.com",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            referral_code="DISABLED",
            accept_toc_and_privacy=True,
        )

        with pytest.raises(HttpError, match="Invalid or inactive referral code."):
            account_service.register_user(payload)

        assert RevelUser.objects.filter(email="referred@example.com").count() == 0

    @patch("accounts.tasks.send_verification_email.delay")
    def test_register_without_referral_code(self, mock_send_email: MagicMock) -> None:
        """Test that registration without a referral code creates no Referral."""
        payload = schema.RegisterUserSchema(
            email="noreferral@example.com",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            accept_toc_and_privacy=True,
        )

        user, _ = account_service.register_user(payload)

        assert not Referral.objects.filter(referred_user=user).exists()

    def test_register_with_empty_referral_code_fails(self) -> None:
        """Test that an explicitly provided empty referral code raises 422."""
        payload = schema.RegisterUserSchema(
            email="referred@example.com",
            password1="a-Strong-password-123!",
            password2="a-Strong-password-123!",
            referral_code="",
            accept_toc_and_privacy=True,
        )

        with pytest.raises(HttpError, match="Invalid or inactive referral code."):
            account_service.register_user(payload)

        assert RevelUser.objects.filter(email="referred@example.com").count() == 0
