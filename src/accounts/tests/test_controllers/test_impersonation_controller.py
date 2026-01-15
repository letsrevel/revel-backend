"""Tests for the impersonation API endpoint."""

import time
from datetime import timedelta
from unittest.mock import patch

import jwt
import orjson
import pytest
from django.conf import settings
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from accounts.service.impersonation import create_impersonation_request

pytestmark = pytest.mark.django_db


class TestImpersonateEndpoint:
    """Tests for POST /api/auth/impersonate endpoint."""

    def test_successful_impersonation(self, client: Client, superuser: RevelUser, user: RevelUser) -> None:
        """Should exchange a valid request token for an access token."""
        # Create impersonation request token
        token, _log = create_impersonation_request(admin=superuser, target=user)

        url = reverse("api:impersonate")
        response = client.post(
            url,
            data=orjson.dumps({"token": token}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()

        assert "access_token" in data
        assert data["expires_in"] == 900  # 15 minutes
        assert data["user"]["id"] == str(user.id)
        assert data["user"]["email"] == user.email
        assert data["user"]["display_name"] == user.display_name
        assert data["impersonated_by"] == superuser.email

    def test_access_token_contains_impersonation_claims(
        self, client: Client, superuser: RevelUser, user: RevelUser
    ) -> None:
        """Returned access token should contain impersonation metadata."""
        token, _log = create_impersonation_request(admin=superuser, target=user)

        url = reverse("api:impersonate")
        response = client.post(
            url,
            data=orjson.dumps({"token": token}),
            content_type="application/json",
        )

        assert response.status_code == 200
        access_token = response.json()["access_token"]

        # Decode and verify claims
        payload = jwt.decode(access_token, options={"verify_signature": False})

        assert payload["is_impersonated"] is True
        assert payload["impersonated_by_id"] == str(superuser.id)
        assert payload["impersonated_by_email"] == superuser.email
        assert payload["sub"] == str(user.id)

    def test_invalid_token_returns_401(self, client: Client) -> None:
        """Should return 401 for invalid tokens."""
        url = reverse("api:impersonate")
        response = client.post(
            url,
            data=orjson.dumps({"token": "invalid.token.here"}),
            content_type="application/json",
        )

        assert response.status_code == 401

    def test_expired_token_returns_401(self, client: Client, superuser: RevelUser, user: RevelUser) -> None:
        """Should return 401 for expired tokens."""
        # Create token with very short lifetime
        with patch.object(
            settings,
            "IMPERSONATION_REQUEST_TOKEN_LIFETIME",
            timedelta(seconds=1),
        ):
            token, _log = create_impersonation_request(admin=superuser, target=user)

        # Wait for expiration
        time.sleep(2)

        url = reverse("api:impersonate")
        response = client.post(
            url,
            data=orjson.dumps({"token": token}),
            content_type="application/json",
        )

        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()

    def test_reused_token_returns_401(self, client: Client, superuser: RevelUser, user: RevelUser) -> None:
        """Should return 401 when token is reused."""
        token, _log = create_impersonation_request(admin=superuser, target=user)
        url = reverse("api:impersonate")

        # First request succeeds
        response1 = client.post(
            url,
            data=orjson.dumps({"token": token}),
            content_type="application/json",
        )
        assert response1.status_code == 200

        # Second request fails (token is blacklisted after first use)
        response2 = client.post(
            url,
            data=orjson.dumps({"token": token}),
            content_type="application/json",
        )
        assert response2.status_code == 401
        assert "blacklisted" in response2.json()["detail"].lower()

    def test_target_became_staff_returns_403(self, client: Client, superuser: RevelUser, user: RevelUser) -> None:
        """Should return 403 if target user became staff after token creation."""
        token, _log = create_impersonation_request(admin=superuser, target=user)

        # User becomes staff
        user.is_staff = True
        user.save()

        url = reverse("api:impersonate")
        response = client.post(
            url,
            data=orjson.dumps({"token": token}),
            content_type="application/json",
        )

        assert response.status_code == 403

    def test_missing_token_returns_422(self, client: Client) -> None:
        """Should return 422 for missing token in request body."""
        url = reverse("api:impersonate")
        response = client.post(
            url,
            data=orjson.dumps({}),
            content_type="application/json",
        )

        assert response.status_code == 422

    def test_no_auth_required(self, client: Client, superuser: RevelUser, user: RevelUser) -> None:
        """Endpoint should not require authentication (token is the auth)."""
        token, _log = create_impersonation_request(admin=superuser, target=user)

        # Using unauthenticated client
        url = reverse("api:impersonate")
        response = client.post(
            url,
            data=orjson.dumps({"token": token}),
            content_type="application/json",
        )

        # Should succeed without auth header
        assert response.status_code == 200


class TestImpersonationAuditLog:
    """Tests for impersonation audit logging."""

    def test_redemption_updates_log(self, client: Client, superuser: RevelUser, user: RevelUser) -> None:
        """Redeeming token should update the audit log with redeemed_at."""
        token, log = create_impersonation_request(
            admin=superuser,
            target=user,
            ip_address="10.0.0.1",
            user_agent="Test/1.0",
        )

        assert log.redeemed_at is None

        url = reverse("api:impersonate")
        response = client.post(
            url,
            data=orjson.dumps({"token": token}),
            content_type="application/json",
        )

        assert response.status_code == 200

        log.refresh_from_db()
        assert log.redeemed_at is not None
        assert log.admin_user == superuser  # type: ignore[unreachable]
        assert log.target_user == user
        assert log.ip_address == "10.0.0.1"
        assert log.user_agent == "Test/1.0"
