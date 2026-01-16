"""Apple Wallet pass signing using PKCS#7.

This module handles the cryptographic signing of Apple Wallet passes.
A .pkpass file requires a PKCS#7 detached signature of the manifest.json
file, signed with the Pass Type ID certificate and including the Apple
WWDR (Worldwide Developer Relations) intermediate certificate.
"""

import hashlib
import json
import typing as t
from pathlib import Path

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7
from django.conf import settings

logger = structlog.get_logger(__name__)


class ApplePassSignerError(Exception):
    """Raised when pass signing fails."""


class ApplePassSigner:
    """Signs Apple Wallet passes using PKCS#7.

    This class handles loading certificates and keys, creating manifests,
    and generating the PKCS#7 signature required for .pkpass files.
    """

    def __init__(
        self,
        cert_path: str | None = None,
        key_path: str | None = None,
        key_password: str | None = None,
        wwdr_cert_path: str | None = None,
    ) -> None:
        """Initialize the signer with certificate paths.

        Args:
            cert_path: Path to the Pass Type ID certificate (PEM format).
            key_path: Path to the private key (PEM format).
            key_password: Password for the private key (if encrypted).
            wwdr_cert_path: Path to Apple WWDR intermediate certificate.

        If paths are not provided, they are read from Django settings.
        """
        self.cert_path = cert_path or settings.APPLE_WALLET_CERT_PATH
        self.key_path = key_path or settings.APPLE_WALLET_KEY_PATH
        self.key_password = key_password or settings.APPLE_WALLET_KEY_PASSWORD
        self.wwdr_cert_path = wwdr_cert_path or settings.APPLE_WALLET_WWDR_CERT_PATH

        self._certificate: x509.Certificate | None = None
        self._private_key: t.Any = None
        self._wwdr_certificate: x509.Certificate | None = None

    def _load_certificate(self, path: str) -> x509.Certificate:
        """Load an X.509 certificate from a PEM file.

        Args:
            path: Path to the certificate file.

        Returns:
            The loaded certificate.

        Raises:
            ApplePassSignerError: If the certificate cannot be loaded.
        """
        try:
            cert_path = Path(path)
            cert_data = cert_path.read_bytes()
            return x509.load_pem_x509_certificate(cert_data)
        except FileNotFoundError:
            raise ApplePassSignerError(f"Certificate not found: {path}")
        except Exception as e:
            raise ApplePassSignerError(f"Failed to load certificate {path}: {e}")

    def _load_private_key(self, path: str, password: str | None = None) -> t.Any:
        """Load a private key from a PEM file.

        Args:
            path: Path to the private key file.
            password: Password for encrypted keys.

        Returns:
            The loaded private key.

        Raises:
            ApplePassSignerError: If the key cannot be loaded.
        """
        try:
            key_path = Path(path)
            key_data = key_path.read_bytes()
            password_bytes = password.encode() if password else None
            return serialization.load_pem_private_key(key_data, password=password_bytes)
        except FileNotFoundError:
            raise ApplePassSignerError(f"Private key not found: {path}")
        except Exception as e:
            raise ApplePassSignerError(f"Failed to load private key {path}: {e}")

    @property
    def certificate(self) -> x509.Certificate:
        """Get the Pass Type ID certificate, loading if necessary."""
        if self._certificate is None:
            self._certificate = self._load_certificate(self.cert_path)
        return self._certificate

    @property
    def private_key(self) -> t.Any:
        """Get the private key, loading if necessary."""
        if self._private_key is None:
            self._private_key = self._load_private_key(self.key_path, self.key_password)
        return self._private_key

    @property
    def wwdr_certificate(self) -> x509.Certificate:
        """Get the Apple WWDR intermediate certificate, loading if necessary."""
        if self._wwdr_certificate is None:
            self._wwdr_certificate = self._load_certificate(self.wwdr_cert_path)
        return self._wwdr_certificate

    def create_manifest(self, files: dict[str, bytes]) -> bytes:
        """Create the manifest.json content for a pass.

        The manifest contains SHA-1 hashes of all files in the pass package.

        Args:
            files: Dictionary mapping filenames to their content bytes.

        Returns:
            The manifest.json content as bytes.
        """
        manifest: dict[str, str] = {}

        for filename, content in files.items():
            # Skip manifest and signature files themselves
            if filename in ("manifest.json", "signature"):
                continue
            sha1_hash = hashlib.sha1(content).hexdigest()
            manifest[filename] = sha1_hash

        return json.dumps(manifest, indent=2).encode("utf-8")

    def sign_manifest(self, manifest_data: bytes) -> bytes:
        """Create a PKCS#7 detached signature of the manifest.

        Uses the cryptography library to create a PKCS#7 signature with SHA-256.

        Args:
            manifest_data: The manifest.json content to sign.

        Returns:
            The PKCS#7 signature in DER format.

        Raises:
            ApplePassSignerError: If signing fails.
        """
        try:
            signature = (
                pkcs7.PKCS7SignatureBuilder()
                .set_data(manifest_data)
                .add_signer(self.certificate, self.private_key, hashes.SHA256())
                .add_certificate(self.wwdr_certificate)
                .sign(Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary])
            )

            logger.debug(
                "manifest_signed",
                manifest_size=len(manifest_data),
                signature_size=len(signature),
            )

            return signature

        except Exception as e:
            logger.error("manifest_signing_failed", error=str(e))
            raise ApplePassSignerError(f"Failed to sign manifest: {e}")

    def is_configured(self) -> bool:
        """Check if all required certificates are configured.

        Returns:
            True if all certificate paths are set and non-empty.
        """
        return bool(self.cert_path and self.key_path and self.wwdr_cert_path and settings.APPLE_WALLET_PASS_TYPE_ID)

    def validate_configuration(self) -> None:
        """Validate that certificates can be loaded.

        Raises:
            ApplePassSignerError: If any certificate cannot be loaded.
        """
        if not self.is_configured():
            raise ApplePassSignerError(
                "Apple Wallet is not configured. Set APPLE_WALLET_CERT_PATH, "
                "APPLE_WALLET_KEY_PATH, APPLE_WALLET_WWDR_CERT_PATH, and "
                "APPLE_WALLET_PASS_TYPE_ID in settings."
            )

        # Try loading all certificates to validate they're accessible
        _ = self.certificate
        _ = self.private_key
        _ = self.wwdr_certificate

        logger.info("apple_wallet_signer_validated")
