"""HMAC-based URL signing for protected file access.

This module provides utilities for generating and verifying signed URLs
for protected media files served via Caddy's forward_auth directive.

Architecture:
    Client → Caddy → forward_auth → Django /api/media/validate/*
                                         ↓
                            Validates HMAC signature + expiry
                                         ↓
                            Returns 200 (serve file) or 401

URL Format:
    /media/file/abc123.pdf?exp=1704067200&sig=a1b2c3d4e5f6

Why HMAC over MinIO/S3:
    - No additional services (Caddy already serves files)
    - FOSS-friendly: MinIO recently moved to AGPL + source-only distribution
    - Simple is better for our use case (<100MB files, no streaming needed)
    - Avoids vendor lock-in while keeping architecture minimal

Security:
    - Uses Django's SECRET_KEY with a domain-specific prefix for isolation
    - Signatures are 16 hex chars (64 bits) - sufficient for URL signing given:
      1. Rate limiting (100 requests/min per IP)
      2. Short URL validity (1 hour default)
      3. Use case is access control, not authentication
    - Uses hmac.compare_digest() to prevent timing attacks
    - Timestamps prevent indefinite URL reuse

Protected Paths:
    Any file path starting with 'protected/' requires signed URL access.
    Use ProtectedFileField or ProtectedImageField to ensure files are
    stored in the protected/ directory. See common/fields.py.
"""

import hashlib
import hmac
import time
import typing as t
from functools import lru_cache
from urllib.parse import urlencode

from django.conf import settings
from django.db.models.fields.files import FieldFile

__all__ = [
    "PROTECTED_PATH_PREFIX",
    "DEFAULT_EXPIRES_IN",
    "generate_signature",
    "verify_signature",
    "generate_signed_url",
    "is_protected_path",
    "get_file_url",
    "parse_signed_url_params",
    "SignedURLParams",
]

# Signature length in hex characters (64 bits = 16 hex chars)
# This provides sufficient security for URL signing while keeping URLs short
SIGNATURE_LENGTH = 16

# Default URL expiration in seconds (1 hour)
DEFAULT_EXPIRES_IN = 3600

# Domain separator for key derivation
# Ensures URL signing key is isolated from other SECRET_KEY uses
_KEY_DOMAIN = "revel:signed-url:v1"

# Prefix for protected file paths.
# Files with paths starting with this prefix require signed URL access.
# This must match the Caddy forward_auth configuration.
PROTECTED_PATH_PREFIX = "protected/"


@lru_cache(maxsize=1)
def _get_signing_key() -> bytes:
    """Get the signing key, derived from Django's SECRET_KEY.

    Uses HKDF-like domain separation to ensure the signing key
    is cryptographically isolated from other uses of SECRET_KEY.

    The key is lazily computed on first use and cached for the lifetime
    of the process. This avoids issues with settings not being configured
    at module import time (e.g., during some test setups).

    Returns:
        Bytes suitable for HMAC-SHA256 signing.
    """
    # Simple domain separation: hash(domain || secret_key)
    # This ensures our signing key is distinct from other uses of SECRET_KEY
    return hashlib.sha256(f"{_KEY_DOMAIN}:{settings.SECRET_KEY}".encode()).digest()


def generate_signature(path: str, expires: int) -> str:
    """Generate an HMAC signature for a path and expiration timestamp.

    Args:
        path: The file path (without query string), e.g., "/file/protected/abc.pdf"
        expires: Unix timestamp when the URL expires.

    Returns:
        Hex-encoded signature (truncated to SIGNATURE_LENGTH chars).
    """
    message = f"{path}:{expires}"
    sig = hmac.new(
        _get_signing_key(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()[:SIGNATURE_LENGTH]
    return sig


def verify_signature(path: str, exp: str, sig: str) -> bool:
    """Verify a signed URL signature.

    Args:
        path: The file path that was signed.
        exp: The expiration timestamp as a string.
        sig: The signature to verify.

    Returns:
        True if signature is valid and URL hasn't expired, False otherwise.
    """
    try:
        expires = int(exp)
    except (ValueError, TypeError):
        return False

    # Check expiration first (cheap operation)
    if expires <= time.time():
        return False

    # Generate expected signature and compare
    expected = generate_signature(path, expires)
    return hmac.compare_digest(sig, expected)


def generate_signed_url(
    path: str,
    *,
    expires_in: int = DEFAULT_EXPIRES_IN,
) -> str:
    """Generate a signed URL for protected file access.

    Args:
        path: The relative file path, e.g., "file/abc.pdf"
        expires_in: Seconds until URL expires (default: 1 hour).

    Returns:
        Full signed URL path with query parameters.

    Example:
        >>> generate_signed_url("file/abc.pdf")
        "/media/file/abc.pdf?exp=1704067200&sig=a1b2c3d4e5f6"
    """
    expires = int(time.time()) + expires_in

    # Build the full path that will be signed
    # Use MEDIA_URL as the base (typically "/media/")
    media_url = settings.MEDIA_URL.rstrip("/")
    full_path = f"{media_url}/{path}" if not path.startswith("/") else f"{media_url}{path}"

    sig = generate_signature(full_path, expires)

    query = urlencode({"exp": expires, "sig": sig})
    return f"{full_path}?{query}"


def is_protected_path(file_path: str) -> bool:
    """Check if a file path requires signed URL access.

    Args:
        file_path: The relative file path, e.g., "protected/file/abc123.pdf".

    Returns:
        True if the path starts with 'protected/'.
    """
    if not file_path:
        return False
    return file_path.startswith(PROTECTED_PATH_PREFIX)


def get_file_url(file_field: FieldFile | None) -> str | None:
    """Get the URL for a file field, signing if it's a protected path.

    This is the main helper for schemas to use when exposing file URLs.
    It automatically determines whether to sign based on the upload path.

    Args:
        file_field: A Django FileField/ImageField value, or None.

    Returns:
        Signed URL for protected paths, direct URL for public paths,
        None if no file.

    Example:
        >>> # In a schema resolver:
        >>> @staticmethod
        >>> def resolve_file_url(obj: MyModel) -> str | None:
        >>>     return get_file_url(obj.file)
    """
    if not file_field:
        return None

    file_path: str | None = file_field.name
    if not file_path:
        return None

    if is_protected_path(file_path):
        return generate_signed_url(file_path)
    return f"{settings.MEDIA_URL}{file_path}"


class SignedURLParams(t.NamedTuple):
    """Parsed signed URL parameters."""

    path: str
    exp: str
    sig: str


def parse_signed_url_params(
    full_path: str,
    exp: str | None,
    sig: str | None,
) -> SignedURLParams | None:
    """Parse and validate signed URL parameters.

    Args:
        full_path: The full request path.
        exp: Expiration timestamp from query string.
        sig: Signature from query string.

    Returns:
        SignedURLParams if all parameters present, None otherwise.
    """
    if not exp or not sig:
        return None
    return SignedURLParams(path=full_path, exp=exp, sig=sig)
