"""Tests for wallet/apple/generator.py."""

import io
import json
import typing as t
import zipfile
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketTier
from wallet.apple.formatting import PassColors
from wallet.apple.generator import ApplePassGenerator, ApplePassGeneratorError, PassData
from wallet.apple.images import ICON_SIZES, LOGO_SIZES
from wallet.apple.signer import ApplePassSignerError

pytestmark = pytest.mark.django_db


# --- Fixtures for generator tests ---


@pytest.fixture
def event_with_address(event: Event) -> Event:
    """Ensure the event fixture has an address for venue testing."""
    event.address = "123 Test Street, Test City"
    event.save()
    return event


@pytest.fixture
def paid_ticket_tier(event_with_address: Event) -> TicketTier:
    """A paid ticket tier for wallet generator tests."""
    return TicketTier.objects.create(
        event=event_with_address,
        name="Premium Wallet Tier",
        price=25.00,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def free_ticket_tier(event_with_address: Event) -> TicketTier:
    """A free ticket tier for wallet generator tests."""
    return TicketTier.objects.create(
        event=event_with_address,
        name="Free Entry",
        price=0,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def wallet_ticket(event_with_address: Event, member_user: RevelUser, paid_ticket_tier: TicketTier) -> Ticket:
    """A paid ticket for wallet generator tests."""
    return Ticket.objects.create(
        event=event_with_address,
        user=member_user,
        tier=paid_ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=member_user.get_display_name(),
    )


@pytest.fixture
def wallet_free_ticket(event_with_address: Event, member_user: RevelUser, free_ticket_tier: TicketTier) -> Ticket:
    """A free ticket for wallet generator tests."""
    return Ticket.objects.create(
        event=event_with_address,
        user=member_user,
        tier=free_ticket_tier,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=member_user.get_display_name(),
    )


class TestPassData:
    """Tests for PassData dataclass."""

    def test_pass_data_creation(self) -> None:
        """Should create PassData with all required fields."""
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")

        data = PassData(
            serial_number="test-123",
            description="Test ticket",
            organization_name="Test Org",
            event_name="Test Event",
            event_start=now,
            event_end=now + timedelta(hours=3),
            address="123 Test St",
            ticket_tier="VIP",
            ticket_price="EUR 25.00",
            colors=colors,
            logo_image=b"logo_bytes",
            barcode_message="barcode-123",
            relevant_date=now,
            venue_name="Test Venue",
        )

        assert data.serial_number == "test-123"
        assert data.description == "Test ticket"
        assert data.organization_name == "Test Org"
        assert data.event_name == "Test Event"
        assert data.venue_name == "Test Venue"
        assert data.address == "123 Test St"
        assert data.ticket_tier == "VIP"
        assert data.ticket_price == "EUR 25.00"
        assert data.barcode_message == "barcode-123"

    def test_pass_data_optional_fields(self) -> None:
        """Should handle optional fields correctly."""
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")

        data = PassData(
            serial_number="test-123",
            description="Test ticket",
            organization_name="Test Org",
            event_name="Test Event",
            event_start=now,
            event_end=now + timedelta(hours=3),
            address=None,
            ticket_tier="General",
            ticket_price="Free",
            colors=colors,
            logo_image=b"logo_bytes",
        )

        assert data.address is None
        assert data.venue_name is None
        assert data.barcode_message == ""
        assert data.relevant_date is None


class TestApplePassGeneratorInit:
    """Tests for ApplePassGenerator initialization."""

    def test_init_creates_default_signer(self) -> None:
        """Should create a default signer if none provided."""
        generator = ApplePassGenerator()
        assert generator.signer is not None

    def test_init_uses_provided_signer(self, mock_signer: MagicMock) -> None:
        """Should use provided signer."""
        generator = ApplePassGenerator(signer=mock_signer)
        assert generator.signer is mock_signer

    def test_init_reads_settings(self, settings: t.Any) -> None:
        """Should read pass type ID and team ID from settings."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test.app"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator()

        assert generator._pass_type_id == "pass.com.test.app"
        assert generator._team_id == "TEAM123"


class TestApplePassGeneratorBuildPassData:
    """Tests for _build_pass_data method."""

    def test_builds_pass_data_from_ticket(
        self,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should build PassData from a Ticket model."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.serial_number == str(wallet_ticket.id)
        assert data.organization_name == wallet_ticket.event.organization.name
        assert data.event_name == wallet_ticket.event.name
        assert data.ticket_tier == wallet_ticket.tier.name
        assert data.barcode_message == str(wallet_ticket.id)

    def test_builds_pass_data_with_free_ticket(
        self,
        wallet_free_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should show 'Free' for zero price tickets."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_free_ticket)

        assert data.ticket_price == "Free"

    def test_builds_pass_data_with_address(
        self,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should include address when event.address is present."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.address == wallet_ticket.event.address

    def test_builds_pass_data_without_address(
        self,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should set address to None when event.address is empty."""
        wallet_ticket.event.address = ""
        wallet_ticket.event.save()

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.address is None

    def test_builds_pass_data_with_relevant_date(
        self,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should set relevant_date to event start."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.relevant_date == wallet_ticket.event.start


class TestApplePassGeneratorBuildPassJson:
    """Tests for _build_pass_json method."""

    def test_builds_valid_json(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """Should build valid JSON bytes."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test.app"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        now = timezone.now()
        colors = PassColors(background="rgb(20,18,28)", foreground="rgb(241,240,243)", label="rgb(163,157,172)")

        data = PassData(
            serial_number="test-serial",
            description="Test Event Ticket",
            organization_name="Test Org",
            event_name="Test Event",
            event_start=now,
            event_end=now + timedelta(hours=3),
            address="123 Test St",
            ticket_tier="VIP",
            ticket_price="EUR 50.00",
            colors=colors,
            logo_image=b"logo",
            barcode_message="barcode-msg",
            relevant_date=now,
        )

        result = generator._build_pass_json(data)
        pass_dict = json.loads(result)

        assert pass_dict["formatVersion"] == 1
        assert pass_dict["passTypeIdentifier"] == "pass.com.test.app"
        assert pass_dict["teamIdentifier"] == "TEAM123"
        assert pass_dict["serialNumber"] == "test-serial"
        assert pass_dict["description"] == "Test Event Ticket"
        assert pass_dict["organizationName"] == "Test Org"

    def test_includes_barcode(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """Should include QR barcode."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")

        data = PassData(
            serial_number="serial-123",
            description="Test",
            organization_name="Org",
            event_name="Event",
            event_start=now,
            event_end=now + timedelta(hours=1),
            address=None,
            ticket_tier="Tier",
            ticket_price="Free",
            colors=colors,
            logo_image=b"logo",
            barcode_message="barcode-content",
        )

        result = generator._build_pass_json(data)
        pass_dict = json.loads(result)

        assert "barcodes" in pass_dict
        assert len(pass_dict["barcodes"]) == 1
        assert pass_dict["barcodes"][0]["format"] == "PKBarcodeFormatQR"
        assert pass_dict["barcodes"][0]["message"] == "barcode-content"

    def test_includes_event_ticket_structure(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """Should include eventTicket structure with proper fields."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")

        data = PassData(
            serial_number="serial",
            description="Test",
            organization_name="Test Org",
            event_name="Test Event",
            event_start=now,
            event_end=now + timedelta(hours=1),
            address="Test Venue",
            ticket_tier="VIP",
            ticket_price="EUR 100.00",
            colors=colors,
            logo_image=b"logo",
        )

        result = generator._build_pass_json(data)
        pass_dict = json.loads(result)

        assert "eventTicket" in pass_dict
        event_ticket = pass_dict["eventTicket"]

        # Check structure
        assert "headerFields" in event_ticket
        assert "primaryFields" in event_ticket
        assert "secondaryFields" in event_ticket
        assert "auxiliaryFields" in event_ticket
        assert "backFields" in event_ticket

    def test_includes_relevant_date(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """Should include relevantDate when provided."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")

        data = PassData(
            serial_number="serial",
            description="Test",
            organization_name="Org",
            event_name="Event",
            event_start=now,
            event_end=now + timedelta(hours=1),
            address=None,
            ticket_tier="Tier",
            ticket_price="Free",
            colors=colors,
            logo_image=b"logo",
            relevant_date=now,
        )

        result = generator._build_pass_json(data)
        pass_dict = json.loads(result)

        assert "relevantDate" in pass_dict

    def test_omits_relevant_date_when_none(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """Should omit relevantDate when not provided."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")

        data = PassData(
            serial_number="serial",
            description="Test",
            organization_name="Org",
            event_name="Event",
            event_start=now,
            event_end=now + timedelta(hours=1),
            address=None,
            ticket_tier="Tier",
            ticket_price="Free",
            colors=colors,
            logo_image=b"logo",
            relevant_date=None,
        )

        result = generator._build_pass_json(data)
        pass_dict = json.loads(result)

        assert "relevantDate" not in pass_dict


class TestApplePassGeneratorGenerateFiles:
    """Tests for _generate_files method."""

    def test_generates_pass_json(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """Should include pass.json in generated files."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")

        data = PassData(
            serial_number="serial",
            description="Test",
            organization_name="Org",
            event_name="Event",
            event_start=now,
            event_end=now + timedelta(hours=1),
            address=None,
            ticket_tier="Tier",
            ticket_price="Free",
            colors=colors,
            logo_image=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,  # Minimal PNG-like
        )

        files = generator._generate_files(data)

        assert "pass.json" in files
        # Verify it's valid JSON
        json.loads(files["pass.json"])

    def test_generates_all_icons(self, settings: t.Any, mock_signer: MagicMock, sample_logo_bytes: bytes) -> None:
        """Should generate all icon variants."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")

        data = PassData(
            serial_number="serial",
            description="Test",
            organization_name="Org",
            event_name="Event",
            event_start=now,
            event_end=now + timedelta(hours=1),
            address=None,
            ticket_tier="Tier",
            ticket_price="Free",
            colors=colors,
            logo_image=sample_logo_bytes,
        )

        files = generator._generate_files(data)

        for icon_name in ICON_SIZES:
            assert icon_name in files
            # Verify PNG format
            assert files[icon_name][:8] == b"\x89PNG\r\n\x1a\n"

    def test_generates_all_logos(self, settings: t.Any, mock_signer: MagicMock, sample_logo_bytes: bytes) -> None:
        """Should generate all logo variants."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")

        data = PassData(
            serial_number="serial",
            description="Test",
            organization_name="Org",
            event_name="Event",
            event_start=now,
            event_end=now + timedelta(hours=1),
            address=None,
            ticket_tier="Tier",
            ticket_price="Free",
            colors=colors,
            logo_image=sample_logo_bytes,
        )

        files = generator._generate_files(data)

        for logo_name in LOGO_SIZES:
            assert logo_name in files


class TestApplePassGeneratorCreateArchive:
    """Tests for _create_archive method."""

    def test_creates_valid_zip(self, mock_signer: MagicMock) -> None:
        """Should create a valid ZIP archive."""
        generator = ApplePassGenerator(signer=mock_signer)

        files = {
            "pass.json": b'{"test": "data"}',
            "icon.png": b"icon_data",
            "manifest.json": b'{"pass.json": "hash"}',
            "signature": b"signature_bytes",
        }

        archive = generator._create_archive(files)

        # Should be valid ZIP
        with zipfile.ZipFile(io.BytesIO(archive), "r") as zf:
            assert zf.testzip() is None  # No errors

    def test_contains_all_files(self, mock_signer: MagicMock) -> None:
        """Should contain all provided files."""
        generator = ApplePassGenerator(signer=mock_signer)

        files = {
            "pass.json": b'{"test": "data"}',
            "icon.png": b"icon_data",
            "logo.png": b"logo_data",
            "manifest.json": b'{"pass.json": "hash"}',
            "signature": b"signature_bytes",
        }

        archive = generator._create_archive(files)

        with zipfile.ZipFile(io.BytesIO(archive), "r") as zf:
            namelist = zf.namelist()
            for filename in files:
                assert filename in namelist

    def test_file_contents_preserved(self, mock_signer: MagicMock) -> None:
        """Should preserve file contents."""
        generator = ApplePassGenerator(signer=mock_signer)

        expected_content = b'{"formatVersion": 1, "test": "value"}'
        files = {"pass.json": expected_content}

        archive = generator._create_archive(files)

        with zipfile.ZipFile(io.BytesIO(archive), "r") as zf:
            actual_content = zf.read("pass.json")
            assert actual_content == expected_content


class TestApplePassGeneratorGeneratePass:
    """Tests for generate_pass method."""

    def test_generates_complete_pkpass(
        self,
        settings: t.Any,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should generate a complete .pkpass file."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test.app"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        pkpass = generator.generate_pass(wallet_ticket)

        # Should be valid ZIP
        with zipfile.ZipFile(io.BytesIO(pkpass), "r") as zf:
            namelist = zf.namelist()

            # Must contain required files
            assert "pass.json" in namelist
            assert "manifest.json" in namelist
            assert "signature" in namelist

            # Should contain icons
            assert "icon.png" in namelist
            assert "icon@2x.png" in namelist

            # Should contain logos
            assert "logo.png" in namelist
            assert "logo@2x.png" in namelist

    def test_raises_on_signer_error(
        self,
        settings: t.Any,
        wallet_ticket: Ticket,
    ) -> None:
        """Should propagate ApplePassSignerError."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        mock_signer = MagicMock()
        mock_signer.create_manifest.side_effect = ApplePassSignerError("Signing failed")

        generator = ApplePassGenerator(signer=mock_signer)

        with pytest.raises(ApplePassSignerError):
            generator.generate_pass(wallet_ticket)

    def test_raises_generator_error_on_failure(
        self,
        settings: t.Any,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should raise ApplePassGeneratorError on general failure."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)

        # Cause an error during file generation
        with patch.object(generator, "_build_pass_data", side_effect=Exception("Build error")):
            with pytest.raises(ApplePassGeneratorError, match="Failed to generate pass"):
                generator.generate_pass(wallet_ticket)


class TestApplePassGeneratorConstants:
    """Tests for ApplePassGenerator class constants."""

    def test_content_type(self) -> None:
        """Should have correct content type for pkpass."""
        assert ApplePassGenerator.CONTENT_TYPE == "application/vnd.apple.pkpass"

    def test_file_extension(self) -> None:
        """Should have correct file extension."""
        assert ApplePassGenerator.FILE_EXTENSION == "pkpass"
