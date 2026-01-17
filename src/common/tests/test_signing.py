"""Tests for the HMAC URL signing module."""

import time
from unittest.mock import MagicMock

from common.signing import (
    DEFAULT_EXPIRES_IN,
    PROTECTED_PATH_PREFIX,
    SIGNATURE_LENGTH,
    generate_signature,
    generate_signed_url,
    get_file_url,
    is_protected_path,
    parse_signed_url_params,
    verify_signature,
)


class TestGenerateSignature:
    """Tests for generate_signature function."""

    def test_generates_signature_of_correct_length(self) -> None:
        sig = generate_signature("/media/file/test.pdf", int(time.time()) + 3600)
        assert len(sig) == SIGNATURE_LENGTH

    def test_signature_is_hex(self) -> None:
        sig = generate_signature("/media/file/test.pdf", int(time.time()) + 3600)
        # Should only contain hex characters
        assert all(c in "0123456789abcdef" for c in sig)

    def test_same_inputs_produce_same_signature(self) -> None:
        path = "/media/file/test.pdf"
        expires = int(time.time()) + 3600

        sig1 = generate_signature(path, expires)
        sig2 = generate_signature(path, expires)

        assert sig1 == sig2

    def test_different_paths_produce_different_signatures(self) -> None:
        expires = int(time.time()) + 3600

        sig1 = generate_signature("/media/file/test1.pdf", expires)
        sig2 = generate_signature("/media/file/test2.pdf", expires)

        assert sig1 != sig2

    def test_different_expiry_produces_different_signatures(self) -> None:
        path = "/media/file/test.pdf"
        now = int(time.time())

        sig1 = generate_signature(path, now + 3600)
        sig2 = generate_signature(path, now + 7200)

        assert sig1 != sig2


class TestVerifySignature:
    """Tests for verify_signature function."""

    def test_valid_signature_returns_true(self) -> None:
        path = "/media/file/test.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        assert verify_signature(path, str(expires), sig) is True

    def test_expired_signature_returns_false(self) -> None:
        path = "/media/file/test.pdf"
        expires = int(time.time()) - 1  # Already expired
        sig = generate_signature(path, expires)

        assert verify_signature(path, str(expires), sig) is False

    def test_wrong_signature_returns_false(self) -> None:
        path = "/media/file/test.pdf"
        expires = int(time.time()) + 3600

        assert verify_signature(path, str(expires), "wrong_signature") is False

    def test_wrong_path_returns_false(self) -> None:
        path = "/media/file/test.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        assert verify_signature("/media/file/other.pdf", str(expires), sig) is False

    def test_invalid_expiry_returns_false(self) -> None:
        path = "/media/file/test.pdf"
        sig = generate_signature(path, int(time.time()) + 3600)

        assert verify_signature(path, "not_a_number", sig) is False
        assert verify_signature(path, "", sig) is False

    def test_tampered_expiry_returns_false(self) -> None:
        path = "/media/file/test.pdf"
        original_expires = int(time.time()) + 3600
        sig = generate_signature(path, original_expires)

        # Try to extend expiry
        extended_expires = original_expires + 86400
        assert verify_signature(path, str(extended_expires), sig) is False


class TestGenerateSignedUrl:
    """Tests for generate_signed_url function."""

    def test_returns_url_with_signature_params(self) -> None:
        url = generate_signed_url("file/test.pdf")

        assert url.startswith("/media/file/test.pdf?")
        assert "exp=" in url
        assert "sig=" in url

    def test_custom_expiry(self) -> None:
        before = int(time.time())
        url = generate_signed_url("file/test.pdf", expires_in=7200)
        after = int(time.time())

        # Extract exp from URL
        exp_str = url.split("exp=")[1].split("&")[0]
        exp = int(exp_str)

        assert exp >= before + 7200
        assert exp <= after + 7200

    def test_default_expiry(self) -> None:
        before = int(time.time())
        url = generate_signed_url("file/test.pdf")
        after = int(time.time())

        exp_str = url.split("exp=")[1].split("&")[0]
        exp = int(exp_str)

        assert exp >= before + DEFAULT_EXPIRES_IN
        assert exp <= after + DEFAULT_EXPIRES_IN

    def test_generated_url_is_verifiable(self) -> None:
        url = generate_signed_url("file/test.pdf")

        # Parse the URL
        path, query = url.split("?")
        params = dict(param.split("=") for param in query.split("&"))

        assert verify_signature(path, params["exp"], params["sig"]) is True


class TestIsProtectedPath:
    """Tests for is_protected_path function."""

    def test_protected_path_returns_true(self) -> None:
        # Paths starting with "protected/" should be protected
        assert is_protected_path("protected/file/test.pdf") is True
        assert is_protected_path("protected/file/subdir/test.pdf") is True
        assert is_protected_path("protected/questionnaire_files/user123/doc.pdf") is True

    def test_public_path_returns_false(self) -> None:
        # Paths not starting with "protected/" should not be protected
        assert is_protected_path("logos/org-logo.png") is False
        assert is_protected_path("cover-art/event.jpg") is False
        assert is_protected_path("file/test.pdf") is False  # Old path format

    def test_empty_path_returns_false(self) -> None:
        assert is_protected_path("") is False

    def test_protected_prefix_constant(self) -> None:
        # Ensure PROTECTED_PATH_PREFIX is what we expect
        assert PROTECTED_PATH_PREFIX == "protected/"


class TestGetFileUrl:
    """Tests for get_file_url function."""

    def test_returns_none_for_none_input(self) -> None:
        assert get_file_url(None) is None

    def test_returns_none_for_empty_file(self) -> None:
        # Mock a file field that evaluates to falsy
        mock_file = MagicMock()
        mock_file.__bool__ = lambda self: False
        assert get_file_url(mock_file) is None

    def test_returns_signed_url_for_protected_path(self) -> None:
        mock_file = MagicMock()
        mock_file.name = "protected/file/test.pdf"

        url = get_file_url(mock_file)

        assert url is not None
        assert url.startswith("/media/protected/file/test.pdf?")
        assert "exp=" in url
        assert "sig=" in url

    def test_returns_direct_url_for_public_path(self) -> None:
        mock_file = MagicMock()
        mock_file.name = "logos/org-logo.png"

        url = get_file_url(mock_file)

        assert url == "/media/logos/org-logo.png"
        assert "exp=" not in url
        assert "sig=" not in url


class TestParseSignedUrlParams:
    """Tests for parse_signed_url_params function."""

    def test_returns_params_when_all_present(self) -> None:
        result = parse_signed_url_params("/media/file/test.pdf", "1234567890", "abcd1234")

        assert result is not None
        assert result.path == "/media/file/test.pdf"
        assert result.exp == "1234567890"
        assert result.sig == "abcd1234"

    def test_returns_none_when_exp_missing(self) -> None:
        result = parse_signed_url_params("/media/file/test.pdf", None, "abcd1234")
        assert result is None

    def test_returns_none_when_sig_missing(self) -> None:
        result = parse_signed_url_params("/media/file/test.pdf", "1234567890", None)
        assert result is None

    def test_returns_none_when_both_missing(self) -> None:
        result = parse_signed_url_params("/media/file/test.pdf", None, None)
        assert result is None

    def test_returns_none_for_empty_strings(self) -> None:
        result = parse_signed_url_params("/media/file/test.pdf", "", "abcd1234")
        assert result is None

        result = parse_signed_url_params("/media/file/test.pdf", "1234567890", "")
        assert result is None


class TestSpecialCharacterPaths:
    """Tests for paths containing special characters."""

    def test_path_with_spaces(self) -> None:
        """Test that paths with spaces are handled correctly."""
        path = "/media/protected/file/document with spaces.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        assert verify_signature(path, str(expires), sig) is True
        # Different path should fail
        assert verify_signature(path.replace(" ", "_"), str(expires), sig) is False

    def test_path_with_unicode(self) -> None:
        """Test that paths with unicode characters are handled correctly."""
        path = "/media/protected/file/documento_español_日本語.pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        assert verify_signature(path, str(expires), sig) is True

    def test_path_with_url_encoded_characters(self) -> None:
        """Test that paths with URL-encoded characters work correctly.

        Note: Paths should be decoded before signing (the actual file path,
        not the URL-encoded version).
        """
        # Raw path (what the file is actually named)
        raw_path = "/media/protected/file/doc (1).pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(raw_path, expires)

        assert verify_signature(raw_path, str(expires), sig) is True

    def test_path_with_special_filesystem_chars(self) -> None:
        """Test paths with characters that are valid in paths."""
        path = "/media/protected/file/doc-name_v2.0+final[edited].pdf"
        expires = int(time.time()) + 3600
        sig = generate_signature(path, expires)

        assert verify_signature(path, str(expires), sig) is True

    def test_signed_url_with_special_chars(self) -> None:
        """Test that generate_signed_url works with special characters."""
        # Note: This tests URL generation, not the signing itself
        url = generate_signed_url("protected/file/doc with spaces.pdf")

        assert "doc with spaces.pdf" in url
        assert "exp=" in url
        assert "sig=" in url

        # Parse and verify the generated URL
        path, query = url.split("?")
        params = dict(param.split("=") for param in query.split("&"))
        assert verify_signature(path, params["exp"], params["sig"]) is True

    def test_get_file_url_with_special_chars(self) -> None:
        """Test get_file_url helper with special character paths."""
        mock_file = MagicMock()
        mock_file.name = "protected/file/résumé (2024).pdf"

        url = get_file_url(mock_file)

        assert url is not None
        assert "résumé (2024).pdf" in url
        assert "exp=" in url
        assert "sig=" in url
