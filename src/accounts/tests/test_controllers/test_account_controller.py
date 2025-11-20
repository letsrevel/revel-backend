## src/accounts/tests/test_controllers/test_account_controller.py
"""test_account_controller.py: Integration tests for the AccountController."""

from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from ninja_jwt.schema import TokenObtainPairOutputSchema

from accounts import schema
from accounts.models import RevelUser

pytestmark = pytest.mark.django_db


@patch("accounts.service.account.register_user")
def test_register_success(
    mock_register: MagicMock, client: Client, valid_register_payload: schema.RegisterUserSchema
) -> None:
    """Test successful user registration returns 201."""
    mock_user = RevelUser(
        username=valid_register_payload.email,
        email=valid_register_payload.email,
        first_name=valid_register_payload.first_name,
    )
    mock_register.return_value = (mock_user, "fake-token")

    url = reverse("api:register-account")
    response = client.post(url, data=valid_register_payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 201
    mock_register.assert_called_once()
    data = response.json()
    assert data["email"] == valid_register_payload.email


def test_register_password_mismatch(client: Client, valid_register_payload: schema.RegisterUserSchema) -> None:
    """Test registration with mismatched passwords returns 422."""
    payload = valid_register_payload.model_copy(update={"password2": "different-password"})
    url = reverse("api:register-account")
    response = client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 422  # Ninja validation error
    assert "Passwords do not match" in response.json()["detail"][0]["msg"]


@patch("accounts.service.account.verify_email")
@patch("accounts.controllers.account.get_token_pair_for_user")
def test_verify_email_success(
    mock_get_token: MagicMock, mock_verify: MagicMock, client: Client, user: RevelUser
) -> None:
    """Test that a valid verification token returns a 200 and a token pair."""
    mock_verify.return_value = user
    mock_get_token.return_value = TokenObtainPairOutputSchema(
        access="access_token", refresh="refresh_token", username=user.username
    )
    url = reverse("api:verify-email")
    payload = {"token": "valid.verification.token"}

    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mock_verify.assert_called_once_with("valid.verification.token")
    mock_get_token.assert_called_once_with(user)
    data = response.json()
    assert data["user"]["email"] == user.email
    assert data["token"]["access"] == "access_token"


@patch("accounts.tasks.send_verification_email.delay")
def test_resend_verification_email_success(
    mock_send_email: MagicMock, client: Client, unverified_user: RevelUser
) -> None:
    """Test resending verification for an unverified user returns 200."""
    url = reverse("api:resend-verification-email")
    payload = {"email": unverified_user.email}
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["message"] == "Verification email sent."
    mock_send_email.assert_called_once()


@patch("accounts.tasks.send_verification_email.delay")
def test_resend_verification_email_for_verified_user(
    mock_send_email: MagicMock, client: Client, user: RevelUser
) -> None:
    """Test trying to resend for an already verified user returns 200 (security by obscurity)."""
    user.email_verified = True
    user.save()
    url = reverse("api:resend-verification-email")
    payload = {"email": user.email}
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["message"] == "Verification email sent."
    # Email should NOT be sent for already verified users
    mock_send_email.assert_not_called()


@patch("accounts.tasks.send_verification_email.delay")
def test_resend_verification_email_nonexistent_user(mock_send_email: MagicMock, client: Client) -> None:
    """Test resending verification for a non-existent user returns 200 (security by obscurity)."""
    url = reverse("api:resend-verification-email")
    payload = {"email": "nonexistent@example.com"}
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["message"] == "Verification email sent."
    # Email should NOT be sent for non-existent users
    mock_send_email.assert_not_called()


@patch("accounts.tasks.send_account_deletion_link.delay")
def test_delete_account_request(mock_send_email: MagicMock, auth_client: Client) -> None:
    """Test that requesting account deletion returns 200."""
    url = reverse("api:delete-account-request")
    response = auth_client.post(url)

    assert response.status_code == 200
    assert response.json()["message"] == "An email has been sent."
    mock_send_email.assert_called_once()


@patch("accounts.service.account.confirm_account_deletion")
def test_delete_account_confirm(mock_confirm_delete: MagicMock, client: Client) -> None:
    """Test that confirming deletion returns 200."""
    url = reverse("api:delete-account-confirm")
    payload = {"token": "valid.deletion.token"}
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["message"] == "Your account deletion has been initiated and will be processed shortly."
    mock_confirm_delete.assert_called_once_with("valid.deletion.token")


@patch("accounts.tasks.send_password_reset_link.delay")
def test_password_reset_request(mock_send_email: MagicMock, client: Client, user: RevelUser) -> None:
    """Test password reset request returns 200, preventing user enumeration."""
    url = reverse("api:reset-password-request")
    # Test for existing user
    response_existing = client.post(url, data=orjson.dumps({"email": user.email}), content_type="application/json")
    assert response_existing.status_code == 200
    assert "will be sent" in response_existing.json()["message"]
    mock_send_email.assert_called_once()

    # Test for non-existing user
    response_nonexisting = client.post(
        url, data=orjson.dumps({"email": "nosuchuser@example.com"}), content_type="application/json"
    )
    assert response_nonexisting.status_code == 200
    assert "will be sent" in response_nonexisting.json()["message"]
    # Assert that the mock was not called a second time
    mock_send_email.assert_called_once()


@patch("accounts.service.account.reset_password")
def test_password_reset_confirm(mock_reset: MagicMock, client: Client) -> None:
    """Test confirming a password reset returns 200."""
    url = reverse("api:reset-password")
    payload = {
        "token": "valid.reset.token",
        "password1": "a-New-strong-password-123!",
        "password2": "a-New-strong-password-123!",
    }
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["message"] == "Password reset successfully."
    mock_reset.assert_called_once_with("valid.reset.token", "a-New-strong-password-123!")


def test_update_profile_success(auth_client: Client, user: RevelUser) -> None:
    """Test successful profile update returns 200 with updated data."""
    url = reverse("api:update-profile")
    payload = {
        "preferred_name": "Alex Smith",
        "pronouns": "they/them",
        "first_name": "Alexander",
        "last_name": "Smith Jr.",
        "language": "en",
    }

    response = auth_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["preferred_name"] == "Alex Smith"
    assert data["pronouns"] == "they/them"
    assert data["first_name"] == "Alexander"
    assert data["last_name"] == "Smith Jr."

    # Verify the user was actually updated in the database
    user.refresh_from_db()
    assert user.preferred_name == "Alex Smith"
    assert user.pronouns == "they/them"
    assert user.first_name == "Alexander"
    assert user.last_name == "Smith Jr."
