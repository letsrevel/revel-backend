"""Tests for wallet/apple/signer.py."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa

from wallet.apple.signer import ApplePassSigner, ApplePassSignerError


class TestApplePassSignerInit:
    """Tests for ApplePassSigner initialization."""

    def test_init_uses_settings_defaults(self, settings: Any) -> None:
        """Should use Django settings for defaults."""
        settings.APPLE_WALLET_CERT_PATH = "/path/to/cert.pem"
        settings.APPLE_WALLET_KEY_PATH = "/path/to/key.pem"
        settings.APPLE_WALLET_KEY_PASSWORD = "secret"
        settings.APPLE_WALLET_WWDR_CERT_PATH = "/path/to/wwdr.pem"

        signer = ApplePassSigner()

        assert signer.cert_path == "/path/to/cert.pem"
        assert signer.key_path == "/path/to/key.pem"
        assert signer.key_password == "secret"
        assert signer.wwdr_cert_path == "/path/to/wwdr.pem"

    def test_init_overrides_settings(self) -> None:
        """Should allow overriding settings via constructor."""
        signer = ApplePassSigner(
            cert_path="/custom/cert.pem",
            key_path="/custom/key.pem",
            key_password="custom_password",
            wwdr_cert_path="/custom/wwdr.pem",
        )

        assert signer.cert_path == "/custom/cert.pem"
        assert signer.key_path == "/custom/key.pem"
        assert signer.key_password == "custom_password"
        assert signer.wwdr_cert_path == "/custom/wwdr.pem"

    def test_init_certs_not_loaded(self) -> None:
        """Certificates should not be loaded until accessed."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        assert signer._certificate is None
        assert signer._private_key is None
        assert signer._wwdr_certificate is None


class TestApplePassSignerLoadCertificate:
    """Tests for certificate loading."""

    def test_load_certificate_file_not_found(self) -> None:
        """Should raise ApplePassSignerError when file not found."""
        signer = ApplePassSigner(
            cert_path="/nonexistent/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        with pytest.raises(ApplePassSignerError, match="Certificate not found"):
            _ = signer.certificate

    def test_load_private_key_file_not_found(self) -> None:
        """Should raise ApplePassSignerError when key file not found."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/nonexistent/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        with pytest.raises(ApplePassSignerError, match="Private key not found"):
            _ = signer.private_key

    def test_load_certificate_invalid_format(self, tmp_path: Path) -> None:
        """Should raise ApplePassSignerError for invalid certificate format."""
        cert_file = tmp_path / "invalid.pem"
        cert_file.write_text("not a valid certificate")

        signer = ApplePassSigner(
            cert_path=str(cert_file),
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        with pytest.raises(ApplePassSignerError, match="Failed to load certificate"):
            _ = signer.certificate

    def test_certificate_cached_after_load(
        self,
        mock_certificate: x509.Certificate,
    ) -> None:
        """Certificate should be cached after first load."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        with patch.object(signer, "_load_certificate", return_value=mock_certificate) as mock_load:
            # First access
            cert1 = signer.certificate
            # Second access
            cert2 = signer.certificate

            # Should only load once
            mock_load.assert_called_once()
            assert cert1 is cert2

    def test_private_key_cached_after_load(
        self,
        mock_private_key: rsa.RSAPrivateKey,
    ) -> None:
        """Private key should be cached after first load."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        with patch.object(signer, "_load_private_key", return_value=mock_private_key) as mock_load:
            # First access
            key1 = signer.private_key
            # Second access
            key2 = signer.private_key

            # Should only load once
            mock_load.assert_called_once()
            assert key1 is key2


class TestApplePassSignerCreateManifest:
    """Tests for manifest creation."""

    def test_creates_manifest_with_sha1_hashes(self) -> None:
        """Should create manifest with SHA-1 hashes of files."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        files = {
            "pass.json": b'{"formatVersion": 1}',
            "icon.png": b"fake_icon_data",
        }

        manifest = signer.create_manifest(files)
        manifest_dict = json.loads(manifest)

        # Should contain both files
        assert "pass.json" in manifest_dict
        assert "icon.png" in manifest_dict

        # Verify SHA-1 hash format (40 hex characters)
        assert len(manifest_dict["pass.json"]) == 40
        assert len(manifest_dict["icon.png"]) == 40

    def test_excludes_manifest_and_signature(self) -> None:
        """Should not include manifest.json and signature in manifest."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        files = {
            "pass.json": b'{"formatVersion": 1}',
            "manifest.json": b'{"existing": "manifest"}',
            "signature": b"existing_signature",
        }

        manifest = signer.create_manifest(files)
        manifest_dict = json.loads(manifest)

        assert "pass.json" in manifest_dict
        assert "manifest.json" not in manifest_dict
        assert "signature" not in manifest_dict

    def test_manifest_is_deterministic(self) -> None:
        """Same files should produce same manifest."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        files = {
            "pass.json": b'{"formatVersion": 1}',
            "icon.png": b"icon_data",
        }

        manifest1 = signer.create_manifest(files)
        manifest2 = signer.create_manifest(files)

        # Note: dict order might differ, compare parsed
        assert json.loads(manifest1) == json.loads(manifest2)

    def test_empty_files_produces_empty_manifest(self) -> None:
        """Empty file dict should produce empty manifest."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        manifest = signer.create_manifest({})
        assert json.loads(manifest) == {}


class TestApplePassSignerSignManifest:
    """Tests for manifest signing."""

    def test_sign_manifest_returns_bytes(
        self,
        mock_certificate: x509.Certificate,
        mock_private_key: rsa.RSAPrivateKey,
        mock_wwdr_certificate: x509.Certificate,
    ) -> None:
        """Should return signature bytes."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        # Inject mock certificates
        signer._certificate = mock_certificate
        signer._private_key = mock_private_key
        signer._wwdr_certificate = mock_wwdr_certificate

        manifest = b'{"pass.json": "abc123"}'
        signature = signer.sign_manifest(manifest)

        assert isinstance(signature, bytes)
        assert len(signature) > 0

    def test_sign_manifest_produces_der_format(
        self,
        mock_certificate: x509.Certificate,
        mock_private_key: rsa.RSAPrivateKey,
        mock_wwdr_certificate: x509.Certificate,
    ) -> None:
        """Signature should be in DER format (starts with PKCS#7 header)."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        signer._certificate = mock_certificate
        signer._private_key = mock_private_key
        signer._wwdr_certificate = mock_wwdr_certificate

        manifest = b'{"pass.json": "abc123"}'
        signature = signer.sign_manifest(manifest)

        # DER-encoded PKCS#7 typically starts with 0x30 (SEQUENCE tag)
        assert signature[0] == 0x30

    def test_sign_manifest_raises_on_failure(self) -> None:
        """Should raise ApplePassSignerError on signing failure."""
        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        # Set invalid certificate that will cause signing to fail
        signer._certificate = "not_a_certificate"  # type: ignore[assignment]
        signer._private_key = "not_a_key"
        signer._wwdr_certificate = "not_a_cert"  # type: ignore[assignment]

        with pytest.raises(ApplePassSignerError, match="Failed to sign manifest"):
            signer.sign_manifest(b'{"test": "data"}')


class TestApplePassSignerIsConfigured:
    """Tests for is_configured method."""

    def test_returns_true_when_all_paths_set(self, settings: Any) -> None:
        """Should return True when all required paths are set."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"

        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        assert signer.is_configured() is True

    def test_returns_false_when_cert_path_missing(self, settings: Any) -> None:
        """Should return False when cert_path is missing."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"
        settings.APPLE_WALLET_CERT_PATH = ""  # Also clear settings fallback

        signer = ApplePassSigner(
            cert_path="",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        assert signer.is_configured() is False

    def test_returns_false_when_key_path_missing(self, settings: Any) -> None:
        """Should return False when key_path is missing."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"
        settings.APPLE_WALLET_KEY_PATH = ""  # Also clear settings fallback

        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="",
            wwdr_cert_path="/path/wwdr.pem",
        )

        assert signer.is_configured() is False

    def test_returns_false_when_wwdr_path_missing(self, settings: Any) -> None:
        """Should return False when wwdr_cert_path is missing."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"
        settings.APPLE_WALLET_WWDR_CERT_PATH = ""  # Also clear settings fallback

        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="",
        )

        assert signer.is_configured() is False

    def test_returns_false_when_pass_type_id_missing(self, settings: Any) -> None:
        """Should return False when APPLE_WALLET_PASS_TYPE_ID is missing."""
        settings.APPLE_WALLET_PASS_TYPE_ID = ""

        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        assert signer.is_configured() is False


class TestApplePassSignerValidateConfiguration:
    """Tests for validate_configuration method."""

    def test_raises_when_not_configured(self, settings: Any) -> None:
        """Should raise when configuration is incomplete."""
        settings.APPLE_WALLET_PASS_TYPE_ID = ""

        signer = ApplePassSigner(
            cert_path="",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        with pytest.raises(ApplePassSignerError, match="not configured"):
            signer.validate_configuration()

    def test_raises_when_cert_not_loadable(self, settings: Any) -> None:
        """Should raise when certificate cannot be loaded."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"

        signer = ApplePassSigner(
            cert_path="/nonexistent/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        with pytest.raises(ApplePassSignerError):
            signer.validate_configuration()

    def test_succeeds_when_all_certs_loadable(
        self,
        settings: Any,
        mock_certificate: x509.Certificate,
        mock_private_key: rsa.RSAPrivateKey,
        mock_wwdr_certificate: x509.Certificate,
    ) -> None:
        """Should succeed when all certificates are loadable."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"

        signer = ApplePassSigner(
            cert_path="/path/cert.pem",
            key_path="/path/key.pem",
            wwdr_cert_path="/path/wwdr.pem",
        )

        # Mock the loading methods
        signer._certificate = mock_certificate
        signer._private_key = mock_private_key
        signer._wwdr_certificate = mock_wwdr_certificate

        # Should not raise
        signer.validate_configuration()
