"""Tests for the media validation controller."""

import time

import pytest
from django.test import Client

from common.signing import generate_signature


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.mark.django_db
class TestMediaValidationController:
    """Tests for the /api/media/validate endpoint."""

    def test_valid_signature_returns_200(self, client: Client) -> None:
        """Test that valid signature returns 200 OK."""
        path = "/media/protected/file/test.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        response = client.get(f"/api/media/validate/protected/file/test.pdf?exp={expires}&sig={sig}")

        assert response.status_code == 200

    def test_expired_signature_returns_401(self, client: Client) -> None:
        """Test that expired signature returns 401 Unauthorized."""
        path = "/media/protected/file/test.pdf"
        expires = int(time.time()) - 1  # Already expired
        sig = generate_signature(path, expires)

        response = client.get(f"/api/media/validate/protected/file/test.pdf?exp={expires}&sig={sig}")

        assert response.status_code == 401

    def test_invalid_signature_returns_401(self, client: Client) -> None:
        """Test that invalid signature returns 401 Unauthorized."""
        expires = int(time.time()) + 3600

        response = client.get(f"/api/media/validate/protected/file/test.pdf?exp={expires}&sig=invalid")

        assert response.status_code == 401

    def test_missing_exp_returns_401(self, client: Client) -> None:
        """Test that missing exp parameter returns 401 Unauthorized."""
        response = client.get("/api/media/validate/protected/file/test.pdf?sig=abc123")

        assert response.status_code == 401

    def test_missing_sig_returns_401(self, client: Client) -> None:
        """Test that missing sig parameter returns 401 Unauthorized."""
        expires = int(time.time()) + 3600

        response = client.get(f"/api/media/validate/protected/file/test.pdf?exp={expires}")

        assert response.status_code == 401

    def test_missing_all_params_returns_401(self, client: Client) -> None:
        """Test that missing all params returns 401 Unauthorized."""
        response = client.get("/api/media/validate/protected/file/test.pdf")

        assert response.status_code == 401

    def test_tampered_path_returns_401(self, client: Client) -> None:
        """Test that signature for different path returns 401."""
        path = "/media/protected/file/original.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        # Try to use signature with different path
        response = client.get(f"/api/media/validate/protected/file/different.pdf?exp={expires}&sig={sig}")

        assert response.status_code == 401

    def test_nested_path_works(self, client: Client) -> None:
        """Test that nested paths work correctly."""
        path = "/media/protected/file/subdir/nested/test.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        response = client.get(f"/api/media/validate/protected/file/subdir/nested/test.pdf?exp={expires}&sig={sig}")

        assert response.status_code == 200

    def test_path_with_spaces_url_encoded(self, client: Client) -> None:
        """Test that paths with URL-encoded spaces work correctly."""
        # The signature is generated with the decoded path
        path = "/media/protected/file/document with spaces.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        # The request uses URL-encoded path
        response = client.get(
            f"/api/media/validate/protected/file/document%20with%20spaces.pdf?exp={expires}&sig={sig}"
        )

        assert response.status_code == 200

    def test_path_with_unicode_characters(self, client: Client) -> None:
        """Test that paths with unicode characters work correctly."""
        # Path with unicode characters
        path = "/media/protected/file/文档.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        # Django test client handles unicode in paths
        response = client.get(f"/api/media/validate/protected/file/文档.pdf?exp={expires}&sig={sig}")

        assert response.status_code == 200

    def test_path_with_special_characters(self, client: Client) -> None:
        """Test that paths with special characters work correctly."""
        path = "/media/protected/file/doc-name_v1.2.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        response = client.get(f"/api/media/validate/protected/file/doc-name_v1.2.pdf?exp={expires}&sig={sig}")

        assert response.status_code == 200
