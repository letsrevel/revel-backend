# src/accounts/tests/test_auth_service.py

from unittest.mock import MagicMock, patch

import pytest
from ninja_jwt.schema import TokenObtainPairOutputSchema

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
