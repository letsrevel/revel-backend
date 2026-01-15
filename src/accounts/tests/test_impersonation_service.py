"""Tests for the impersonation service layer."""

import time
from datetime import timedelta
from unittest.mock import patch

import jwt
import pytest
from django.conf import settings
from django.utils import timezone
from ninja.errors import HttpError
from ninja_extra.exceptions import AuthenticationFailed

from accounts.jwt import (
    create_impersonation_request_token,
    validate_impersonation_request_token,
)
from accounts.models import ImpersonationLog, RevelUser
from accounts.service.impersonation import (
    can_impersonate,
    create_impersonation_request,
    redeem_impersonation_token,
)

pytestmark = pytest.mark.django_db


class TestCanImpersonate:
    """Tests for the can_impersonate permission check."""

    def test_superuser_can_impersonate_regular_user(self, superuser: RevelUser, user: RevelUser) -> None:
        """Superuser should be able to impersonate a regular user."""
        allowed, error = can_impersonate(superuser, user)
        assert allowed is True
        assert error is None

    def test_non_superuser_cannot_impersonate(self, user: RevelUser, staff_user: RevelUser) -> None:
        """Non-superuser should not be able to impersonate anyone."""
        allowed, error = can_impersonate(user, staff_user)
        assert allowed is False
        assert "Only superusers" in str(error)

    def test_staff_cannot_impersonate(self, staff_user: RevelUser, user: RevelUser) -> None:
        """Staff (non-superuser) should not be able to impersonate."""
        allowed, error = can_impersonate(staff_user, user)
        assert allowed is False
        assert "Only superusers" in str(error)

    def test_cannot_impersonate_superuser(self, superuser: RevelUser, another_superuser: RevelUser) -> None:
        """Cannot impersonate another superuser."""
        allowed, error = can_impersonate(superuser, another_superuser)
        assert allowed is False
        assert "superusers" in str(error)

    def test_cannot_impersonate_staff(self, superuser: RevelUser, staff_user: RevelUser) -> None:
        """Cannot impersonate staff members."""
        allowed, error = can_impersonate(superuser, staff_user)
        assert allowed is False
        assert "staff" in str(error)

    def test_cannot_impersonate_self(self, superuser: RevelUser) -> None:
        """Cannot impersonate yourself."""
        allowed, error = can_impersonate(superuser, superuser)
        assert allowed is False
        assert "yourself" in str(error)

    def test_cannot_impersonate_inactive_user(self, superuser: RevelUser, inactive_user: RevelUser) -> None:
        """Cannot impersonate inactive users."""
        allowed, error = can_impersonate(superuser, inactive_user)
        assert allowed is False
        assert "inactive" in str(error)


class TestCreateImpersonationRequest:
    """Tests for creating impersonation request tokens."""

    def test_creates_token_and_log(self, superuser: RevelUser, user: RevelUser) -> None:
        """Should create a valid token and audit log entry."""
        token, log = create_impersonation_request(
            admin=superuser,
            target=user,
            ip_address="192.168.1.1",
            user_agent="Test Browser",
        )

        # Token should be a valid JWT
        assert isinstance(token, str)
        assert token.count(".") == 2  # JWT has 3 parts

        # Log should be created
        assert isinstance(log, ImpersonationLog)
        assert log.admin_user == superuser
        assert log.target_user == user
        assert log.ip_address == "192.168.1.1"
        assert log.user_agent == "Test Browser"
        assert log.redeemed_at is None
        assert log.token_jti is not None

    def test_raises_error_for_unauthorized_impersonation(self, user: RevelUser, staff_user: RevelUser) -> None:
        """Should raise HttpError if impersonation not allowed."""
        with pytest.raises(HttpError) as exc_info:
            create_impersonation_request(admin=user, target=staff_user)

        assert exc_info.value.status_code == 403

    def test_token_contains_correct_claims(self, superuser: RevelUser, user: RevelUser) -> None:
        """Token should contain admin and target user IDs."""
        token, _log = create_impersonation_request(admin=superuser, target=user)

        # Decode without verification to inspect claims
        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["type"] == "impersonation-request"
        assert payload["admin_user_id"] == str(superuser.id)
        assert payload["target_user_id"] == str(user.id)
        assert "jti" in payload
        assert "exp" in payload


class TestRedeemImpersonationToken:
    """Tests for redeeming impersonation tokens."""

    def test_successful_redemption(self, superuser: RevelUser, user: RevelUser) -> None:
        """Should exchange request token for access token."""
        token, log = create_impersonation_request(admin=superuser, target=user)

        result = redeem_impersonation_token(token)

        assert result.access_token is not None
        assert result.expires_in == 900  # 15 minutes
        assert result.user == user
        assert result.admin_email == superuser.email

        # Log should be marked as redeemed
        log.refresh_from_db()
        assert log.redeemed_at is not None

    def test_access_token_contains_impersonation_claims(self, superuser: RevelUser, user: RevelUser) -> None:
        """Access token should contain impersonation metadata."""
        token, _log = create_impersonation_request(admin=superuser, target=user)
        result = redeem_impersonation_token(token)

        # Decode access token
        payload = jwt.decode(result.access_token, options={"verify_signature": False})

        assert payload["is_impersonated"] is True
        assert payload["impersonated_by_id"] == str(superuser.id)
        assert payload["impersonated_by_email"] == superuser.email
        assert payload["impersonated_by_name"] == superuser.display_name
        assert payload["sub"] == str(user.id)
        assert payload["email"] == user.email

    def test_token_cannot_be_reused(self, superuser: RevelUser, user: RevelUser) -> None:
        """Token should be single-use."""
        token, _log = create_impersonation_request(admin=superuser, target=user)

        # First redemption succeeds
        redeem_impersonation_token(token)

        # Second redemption fails (token is blacklisted after first use)
        with pytest.raises(HttpError) as exc_info:
            redeem_impersonation_token(token)

        assert exc_info.value.status_code == 401
        assert "blacklisted" in str(exc_info.value.message).lower()

    def test_expired_token_rejected(self, superuser: RevelUser, user: RevelUser) -> None:
        """Expired tokens should be rejected."""
        # Create token with very short lifetime
        with patch.object(settings, "IMPERSONATION_REQUEST_TOKEN_LIFETIME", timedelta(seconds=1)):
            token, _log = create_impersonation_request(admin=superuser, target=user)

        # Wait for expiration
        time.sleep(2)

        with pytest.raises(AuthenticationFailed) as exc_info:
            redeem_impersonation_token(token)

        assert "expired" in str(exc_info.value)

    def test_invalid_token_rejected(self) -> None:
        """Invalid tokens should be rejected."""
        with pytest.raises(AuthenticationFailed):
            redeem_impersonation_token("invalid.token.here")

    def test_tampered_token_rejected(self, superuser: RevelUser, user: RevelUser) -> None:
        """Tampered tokens should be rejected."""
        token, _log = create_impersonation_request(admin=superuser, target=user)

        # Tamper with the token
        tampered_token = token[:-10] + "0000000000"

        with pytest.raises(AuthenticationFailed):
            redeem_impersonation_token(tampered_token)

    def test_revalidates_permissions_on_redemption(self, superuser: RevelUser, user: RevelUser) -> None:
        """Should re-check permissions when redeeming (in case user became staff)."""
        token, _log = create_impersonation_request(admin=superuser, target=user)

        # User becomes staff after token creation
        user.is_staff = True
        user.save()

        with pytest.raises(HttpError) as exc_info:
            redeem_impersonation_token(token)

        assert exc_info.value.status_code == 403
        assert "staff" in str(exc_info.value.message)


class TestJWTFunctions:
    """Tests for JWT token creation and validation functions."""

    # Use proper RFC 4122 version 4 UUIDs for testing
    ADMIN_UUID = "00000000-0000-4000-8000-000000000001"
    TARGET_UUID = "00000000-0000-4000-8000-000000000002"

    def test_create_impersonation_request_token(self) -> None:
        """Should create a valid impersonation request token."""
        token = create_impersonation_request_token(
            admin_user_id=self.ADMIN_UUID,
            target_user_id=self.TARGET_UUID,
            jti="unique-jti",
        )

        assert isinstance(token, str)

        # Validate it can be decoded
        payload = jwt.decode(
            token,
            key=settings.SECRET_KEY,
            audience=settings.JWT_AUDIENCE,
            algorithms=[settings.JWT_ALGORITHM],
        )
        assert payload["type"] == "impersonation-request"
        assert payload["admin_user_id"] == self.ADMIN_UUID
        assert payload["target_user_id"] == self.TARGET_UUID
        assert payload["jti"] == "unique-jti"

    def test_validate_impersonation_request_token(self) -> None:
        """Should validate and parse a valid token."""
        token = create_impersonation_request_token(
            admin_user_id=self.ADMIN_UUID,
            target_user_id=self.TARGET_UUID,
            jti="unique-jti",
        )

        payload = validate_impersonation_request_token(token)

        assert payload.type == "impersonation-request"
        assert str(payload.admin_user_id) == self.ADMIN_UUID
        assert str(payload.target_user_id) == self.TARGET_UUID
        assert payload.jti == "unique-jti"

    def test_validate_rejects_wrong_token_type(self) -> None:
        """Should reject tokens with wrong type claim."""
        # Create a token with wrong type
        from accounts.jwt import create_token

        payload = {
            "iss": "https://api.letsrevel.io/",
            "aud": settings.JWT_AUDIENCE,
            "jti": "some-jti",
            "exp": int((timezone.now() + timedelta(minutes=5)).timestamp()),
            "iat": int(timezone.now().timestamp()),
            "type": "wrong-type",
            "admin_user_id": self.ADMIN_UUID,
            "target_user_id": self.TARGET_UUID,
        }
        token = create_token(payload, settings.SECRET_KEY, settings.JWT_ALGORITHM)

        with pytest.raises(AuthenticationFailed) as exc_info:
            validate_impersonation_request_token(token)

        assert "Invalid token type" in str(exc_info.value)
