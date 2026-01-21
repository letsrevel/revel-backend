# src/accounts/tests/test_auth_service.py

from unittest.mock import MagicMock, patch

import pytest
from ninja_jwt.schema import TokenObtainPairOutputSchema

from accounts import schema
from accounts.jwt import validate_otp_jwt
from accounts.models import RevelUser
from accounts.service import auth as auth_service

pytestmark = pytest.mark.django_db


def test_get_temporary_otp_jwt(user: RevelUser) -> None:
    """Test that a valid temporary JWT is created for TOTP login flow."""
    token = auth_service.get_temporary_otp_jwt(user)
    payload = validate_otp_jwt(token)

    assert isinstance(token, str)
    assert payload.user_id == user.id
    assert payload.email == user.email
    assert payload.type == "totp-access"


@patch("pyotp.TOTP.verify", return_value=True)
def test_verify_otp_jwt_success(mock_verify: MagicMock, user: RevelUser) -> None:
    """Test successful verification of a temporary JWT and a valid OTP."""
    temp_token = auth_service.get_temporary_otp_jwt(user)
    otp = "123456"

    verified_user, is_valid = auth_service.verify_otp_jwt(temp_token, otp)

    assert is_valid is True
    assert verified_user.id == user.id
    mock_verify.assert_called_once_with(otp)


@patch("pyotp.TOTP.verify", return_value=False)
def test_verify_otp_jwt_invalid_otp(mock_verify: MagicMock, user: RevelUser) -> None:
    """Test that verification fails with a correct token but an invalid OTP."""
    temp_token = auth_service.get_temporary_otp_jwt(user)
    otp = "654321"

    verified_user, is_valid = auth_service.verify_otp_jwt(temp_token, otp)

    assert is_valid is False
    assert verified_user.id == user.id
    mock_verify.assert_called_once_with(otp)


def test_get_token_pair_for_user(user: RevelUser) -> None:
    """Test that a valid token pair with correct claims is generated for a user."""
    token_pair = auth_service.get_token_pair_for_user(user)

    assert isinstance(token_pair, TokenObtainPairOutputSchema)
    assert "access" in token_pair.model_dump()
    assert "refresh" in token_pair.model_dump()


@patch("accounts.service.auth._verify_oauth2_token")
def test_google_login_new_user(mock_verify_token: MagicMock, settings: MagicMock) -> None:
    """Test Google login for a brand new user."""
    # Setup mock
    mock_id_info = schema.GoogleIDInfo(
        email="newgoogleuser@example.com",
        given_name="New",
        family_name="Googler",
        sub="987654321",
    )
    mock_verify_token.return_value = mock_id_info
    settings.GOOGLE_SSO_STAFF_LIST = []
    settings.GOOGLE_SSO_SUPERUSER_LIST = []

    assert RevelUser.objects.count() == 0

    # Action
    token_pair = auth_service.google_login("fake-id-token")

    # Assertions
    assert RevelUser.objects.count() == 1
    new_user = RevelUser.objects.get(email="newgoogleuser@example.com")
    assert new_user.first_name == "New"
    assert new_user.email_verified is True
    assert new_user.is_staff is False
    assert token_pair.username == new_user.username  # type: ignore[attr-defined]


@patch("accounts.service.auth._verify_oauth2_token")
def test_google_login_existing_user(mock_verify_token: MagicMock, google_user: RevelUser, settings: MagicMock) -> None:
    """Test Google login for a pre-existing user."""
    # Setup mock
    mock_id_info = schema.GoogleIDInfo(
        email=google_user.email,
        given_name="Updated",  # Name is different
        family_name="User",
        sub=google_user.googlessouser.google_id,  # type: ignore[attr-defined]
    )
    mock_verify_token.return_value = mock_id_info
    settings.GOOGLE_SSO_ALWAYS_UPDATE_USER_DATA = True

    assert RevelUser.objects.count() == 1

    # Action
    auth_service.google_login("fake-id-token")

    # Assertions
    assert RevelUser.objects.count() == 1
    google_user.refresh_from_db()
    assert google_user.first_name == "Updated"  # Data was updated
