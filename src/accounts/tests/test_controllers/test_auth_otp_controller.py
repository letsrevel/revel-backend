# src/accounts/tests/test_controllers/test_auth_otp_controllers.py
"""test_auth_otp_controllers.py: Integration tests for AuthController and OtpController."""

from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser

pytestmark = pytest.mark.django_db


# --- AuthController Tests ---


def test_obtain_token_pair_success(client: Client, user: RevelUser) -> None:
    """Test successful token acquisition for a non-TOTP user."""
    url = reverse("api:token_obtain_pair")
    payload = {"username": user.username, "password": "strong-password-123!"}
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert "access" in data
    assert "refresh" in data


def test_obtain_token_pair_for_totp_user(client: Client, totp_user: RevelUser) -> None:
    """Test that a user with TOTP enabled receives a temporary token."""
    url = reverse("api:token_obtain_pair")
    payload = {"username": totp_user.username, "password": "strong-password-123!"}
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["type"] == "otp"


def test_obtain_token_with_invalid_credentials(client: Client, user: RevelUser) -> None:
    """Test that wrong credentials return a 401."""
    url = reverse("api:token_obtain_pair")
    payload = {"username": user.username, "password": "wrong-password"}
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 401


@patch("accounts.service.auth.verify_otp_jwt")
@patch("accounts.service.auth.get_token_pair_for_user")
def test_obtain_token_with_otp_success(
    mock_get_pair: MagicMock, mock_verify: MagicMock, client: Client, totp_user: RevelUser
) -> None:
    """Test successful token acquisition using a temporary token and a valid OTP."""
    mock_verify.return_value = (totp_user, True)  # Simulate successful OTP verification
    mock_get_pair.return_value = {
        "access": "final_access_token",
        "refresh": "final_refresh_token",
        "username": totp_user.username,
    }

    url = reverse("api:token_obtain_pair_otp")
    payload = {"token": "temp.jwt.token", "otp": "123456"}
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["access"] == "final_access_token"
    mock_verify.assert_called_once_with("temp.jwt.token", "123456")


@patch("accounts.service.auth.google_login")
def test_google_login_success(mock_google_login: MagicMock, client: Client) -> None:
    """Test that the google_login endpoint calls the service and returns a token."""
    mock_google_login.return_value = {
        "access": "google_access_token",
        "refresh": "google_refresh_token",
        "username": "username",
    }
    url = reverse("api:google_sso_login")
    payload = {"id_token": "google.id.token"}
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["access"] == "google_access_token"
    mock_google_login.assert_called_once_with("google.id.token")


# --- OtpController Tests ---


@patch("pyotp.totp.TOTP.provisioning_uri")
def test_setup_otp_success(mock_provisioning_uri: MagicMock, auth_client: Client, user: RevelUser) -> None:
    """Test that a user can get a provisioning URI to set up TOTP."""
    mock_provisioning_uri.return_value = f"otpauth://totp/Test:?secret={user.totp_secret}&issuer=Revel"
    url = reverse("api:setup-otp")
    response = auth_client.get(url)

    assert response.status_code == 200
    assert "uri" in response.json()
    mock_provisioning_uri.assert_called_once()


def test_setup_otp_already_enabled(auth_client: Client, user: RevelUser) -> None:
    """Test that a user with TOTP already enabled gets a 400."""
    user.totp_active = True
    user.save()
    url = reverse("api:setup-otp")
    response = auth_client.get(url)
    assert response.status_code == 400
    assert response.json()["detail"] == "OTP is already enabled."


@patch("pyotp.TOTP.verify", return_value=True)
def test_enable_otp_success(mock_verify: MagicMock, auth_client: Client, user: RevelUser) -> None:
    """Test successfully enabling TOTP with a valid code."""
    assert not user.totp_active
    url = reverse("api:enable-otp")
    payload = {"otp": "123456"}
    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.totp_active is True
    mock_verify.assert_called_once_with("123456")  # type: ignore[unreachable]


@patch("pyotp.TOTP.verify", return_value=False)
def test_enable_otp_invalid_code(mock_verify: MagicMock, auth_client: Client, user: RevelUser) -> None:
    """Test that enabling TOTP fails with an invalid code."""
    url = reverse("api:enable-otp")
    payload = {"otp": "654321"}
    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403
    assert "Invalid OTP" in response.json()["detail"]
    user.refresh_from_db()
    assert not user.totp_active


@patch("pyotp.TOTP.verify", return_value=True)
def test_disable_otp_success(mock_verify: MagicMock, auth_client: Client, user: RevelUser) -> None:
    """Test successfully disabling TOTP with a valid code."""
    user.totp_active = True
    user.save()
    url = reverse("api:disable-otp")
    payload = {"otp": "123456"}
    response = auth_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.totp_active is False
