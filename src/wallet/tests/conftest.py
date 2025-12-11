"""Test fixtures for wallet app tests.

This module provides fixtures for testing Apple Wallet pass generation,
including mocked certificates and pre-configured test data.

Note: This module reuses fixtures from src/events/tests/conftest.py for
model fixtures (organization, event, ticket, etc.) and src/conftest.py
for common fixtures. Only wallet-specific fixtures are defined here.
"""

import io
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from PIL import Image

import wallet.controllers
from accounts.models import RevelUser

# --- Mock Certificate Fixtures ---


@pytest.fixture
def mock_private_key() -> rsa.RSAPrivateKey:
    """Generate a mock RSA private key for testing."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def mock_certificate(mock_private_key: rsa.RSAPrivateKey) -> x509.Certificate:
    """Generate a mock X.509 certificate for testing."""
    from datetime import datetime
    from datetime import timezone as dt_timezone

    from cryptography.x509 import CertificateBuilder, Name, NameAttribute

    subject = issuer = Name(
        [
            NameAttribute(NameOID.COUNTRY_NAME, "US"),
            NameAttribute(NameOID.ORGANIZATION_NAME, "Test Org"),
            NameAttribute(NameOID.COMMON_NAME, "Test Certificate"),
        ]
    )

    now = datetime.now(dt_timezone.utc)
    cert = (
        CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(mock_private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .sign(mock_private_key, hashes.SHA256())
    )
    return cert


@pytest.fixture
def mock_wwdr_certificate(mock_private_key: rsa.RSAPrivateKey) -> x509.Certificate:
    """Generate a mock Apple WWDR certificate for testing."""
    from datetime import datetime
    from datetime import timezone as dt_timezone

    from cryptography.x509 import CertificateBuilder, Name, NameAttribute

    subject = issuer = Name(
        [
            NameAttribute(NameOID.COUNTRY_NAME, "US"),
            NameAttribute(NameOID.ORGANIZATION_NAME, "Apple Inc."),
            NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Apple Worldwide Developer Relations"),
            NameAttribute(NameOID.COMMON_NAME, "Apple Worldwide Developer Relations Certification Authority"),
        ]
    )

    now = datetime.now(dt_timezone.utc)
    cert = (
        CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(mock_private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .sign(mock_private_key, hashes.SHA256())
    )
    return cert


@pytest.fixture
def mock_signer(
    mock_certificate: x509.Certificate,
    mock_private_key: rsa.RSAPrivateKey,
    mock_wwdr_certificate: x509.Certificate,
) -> MagicMock:
    """Create a fully mocked ApplePassSigner for testing."""
    from wallet.apple.signer import ApplePassSigner

    signer = MagicMock(spec=ApplePassSigner)
    signer.certificate = mock_certificate
    signer.private_key = mock_private_key
    signer.wwdr_certificate = mock_wwdr_certificate
    signer.is_configured.return_value = True

    # Mock create_manifest to return valid JSON bytes
    def mock_create_manifest(files: dict[str, bytes]) -> bytes:
        import hashlib
        import json

        manifest = {}
        for filename, content in files.items():
            if filename not in ("manifest.json", "signature"):
                manifest[filename] = hashlib.sha1(content).hexdigest()
        return json.dumps(manifest).encode("utf-8")

    signer.create_manifest.side_effect = mock_create_manifest
    signer.sign_manifest.return_value = b"mock_signature_bytes"

    return signer


@pytest.fixture
def patched_signer_certs(
    mock_certificate: x509.Certificate,
    mock_private_key: rsa.RSAPrivateKey,
    mock_wwdr_certificate: x509.Certificate,
) -> Any:
    """Patch ApplePassSigner to use mock certificates.

    This fixture patches the certificate loading methods to return
    mock certificates, avoiding file system access.
    """
    with (
        patch.object(
            target=__import__("wallet.apple.signer", fromlist=["ApplePassSigner"]).ApplePassSigner,
            attribute="_load_certificate",
            return_value=mock_certificate,
        ),
        patch.object(
            target=__import__("wallet.apple.signer", fromlist=["ApplePassSigner"]).ApplePassSigner,
            attribute="_load_private_key",
            return_value=mock_private_key,
        ),
    ):
        yield


# --- Image Fixtures ---


@pytest.fixture
def sample_logo_bytes() -> bytes:
    """Generate a sample logo PNG for testing (100x100 red square).

    This is different from png_bytes which is a minimal 1x1 image.
    This fixture creates a larger image suitable for logo testing.
    """
    img = Image.new("RGB", (100, 100), (255, 0, 0))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


# --- Mock Model Fixtures for Image Testing ---


@pytest.fixture
def mock_organization() -> MagicMock:
    """Create a mock organization for testing image generation.

    Uses MagicMock instead of real model to avoid database access
    in pure unit tests for image generation functions.
    """
    org = MagicMock()
    org.id = UUID("12345678-1234-5678-1234-567812345678")
    org.name = "Test Organization"
    return org


@pytest.fixture
def mock_event(mock_organization: MagicMock) -> MagicMock:
    """Create a mock event for testing.

    Uses MagicMock instead of real model for unit tests
    that don't need database interaction.
    """
    event = MagicMock()
    event.cover_art = None
    event.event_series = None
    event.organization = mock_organization
    mock_organization.cover_art = None
    return event


# --- Controller Test Fixtures ---


@pytest.fixture
def apple_wallet_configured(settings: Any) -> None:
    """Configure Apple Wallet settings for tests."""
    settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.example.test"
    settings.APPLE_WALLET_TEAM_ID = "TEAM123"
    settings.APPLE_WALLET_CERT_PATH = "/path/cert.pem"
    settings.APPLE_WALLET_KEY_PATH = "/path/key.pem"
    settings.APPLE_WALLET_WWDR_CERT_PATH = "/path/wwdr.pem"


@pytest.fixture
def apple_wallet_not_configured(settings: Any) -> None:
    """Clear Apple Wallet settings for tests."""
    settings.APPLE_WALLET_PASS_TYPE_ID = ""
    settings.APPLE_WALLET_TEAM_ID = ""
    settings.APPLE_WALLET_CERT_PATH = ""
    settings.APPLE_WALLET_KEY_PATH = ""
    settings.APPLE_WALLET_WWDR_CERT_PATH = ""


@pytest.fixture
def mock_pass_generator() -> Generator[MagicMock, None, None]:
    """Mock the ApplePassGenerator for controller tests."""
    wallet.controllers._apple_pass_generator = None

    with patch("wallet.controllers.ApplePassGenerator") as MockGenerator:
        mock_gen = MagicMock()
        mock_gen.generate_pass.return_value = b"mock_pkpass_content"
        mock_gen.CONTENT_TYPE = "application/vnd.apple.pkpass"
        MockGenerator.return_value = mock_gen
        yield mock_gen

    wallet.controllers._apple_pass_generator = None


# --- Model and Client Fixtures for Controller Tests ---
# These fixtures follow the same pattern as events/tests/test_controllers/conftest.py


@pytest.fixture
def organization_owner_user(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization owner user."""
    return django_user_model.objects.create_user(
        username="wallet_org_owner",
        email="wallet_owner@example.com",
        password="pass",
        email_verified=True,
    )


@pytest.fixture
def organization(organization_owner_user: Any) -> Any:
    """Organization for wallet tests."""
    from events.models import Organization

    return Organization.objects.create(
        name="Wallet Test Org",
        slug="wallet-test-org",
        owner=organization_owner_user,
    )


@pytest.fixture
def event(organization: Any) -> Any:
    """Event for wallet controller tests."""
    from datetime import timedelta

    from django.utils import timezone

    from events.models import Event

    now = timezone.now()
    return Event.objects.create(
        organization=organization,
        name="Wallet Test Event",
        slug="wallet-test-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=100,
        start=now + timedelta(days=7),
        end=now + timedelta(days=7, hours=3),
        status="open",
        requires_ticket=True,
        address="123 Test Street",
    )


@pytest.fixture
def event_ticket_tier(event: Any) -> Any:
    """Ticket tier for wallet tests."""
    from events.models import TicketTier

    return TicketTier.objects.create(
        event=event,
        name="Wallet General Tier",
        price=10.00,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def member_user(django_user_model: type[RevelUser]) -> RevelUser:
    """Member user for wallet tests."""
    return django_user_model.objects.create_user(
        username="wallet_member",
        email="wallet_member@example.com",
        password="pass",
        email_verified=True,
    )


@pytest.fixture
def nonmember_user(django_user_model: type[RevelUser]) -> RevelUser:
    """Non-member user for wallet tests."""
    return django_user_model.objects.create_user(
        username="wallet_nonmember",
        email="wallet_nonmember@example.com",
        password="pass",
        email_verified=True,
    )


@pytest.fixture
def ticket(event: Any, member_user: Any, event_ticket_tier: Any) -> Any:
    """Ticket for wallet controller tests."""
    from events.models import Ticket

    return Ticket.objects.create(
        event=event,
        user=member_user,
        tier=event_ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=member_user.get_display_name(),
    )


@pytest.fixture
def member_client(member_user: Any, organization: Any) -> Any:
    """API client for a member user."""
    from django.test.client import Client
    from ninja_jwt.tokens import RefreshToken

    from events.models import OrganizationMember

    OrganizationMember.objects.create(organization=organization, user=member_user)
    refresh = RefreshToken.for_user(member_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def nonmember_client(nonmember_user: Any) -> Any:
    """API client for a non-member user."""
    from django.test.client import Client
    from ninja_jwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(nonmember_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
