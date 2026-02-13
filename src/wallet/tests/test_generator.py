"""Tests for wallet/apple/generator.py."""

import io
import json
import typing as t
import zipfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Ticket, TicketTier
from events.models.ticket import Payment
from events.models.venue import Venue
from wallet.apple.formatting import PassColors
from wallet.apple.generator import PASS_EXPIRATION_GRACE_PERIOD, ApplePassGenerator, ApplePassGeneratorError, PassData
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
        assert data.relevant_date == wallet_ticket.event.start
        assert data.expiration_date == wallet_ticket.event.end + PASS_EXPIRATION_GRACE_PERIOD

    def test_builds_pass_data_with_free_ticket(
        self,
        wallet_free_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should show 'Free' for zero price tickets."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_free_ticket)

        assert data.ticket_price == "Free"

    def test_builds_pass_data_with_event_address_fallback(
        self,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should fall back to event.address when no venue has an address."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        # No venue set, so falls back to event.address
        assert data.address == wallet_ticket.event.address

    def test_builds_pass_data_without_address(
        self,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should set address to None when no address is available."""
        wallet_ticket.event.address = ""
        wallet_ticket.event.save()

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.address is None


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

    def test_includes_optional_dates(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """Should include relevantDate and expirationDate when provided."""
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
            expiration_date=now + timedelta(hours=13),
        )

        result = generator._build_pass_json(data)
        pass_dict = json.loads(result)

        assert "relevantDate" in pass_dict
        assert "expirationDate" in pass_dict

    def test_omits_optional_dates_when_none(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """Should omit relevantDate and expirationDate when not provided."""
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
        )

        result = generator._build_pass_json(data)
        pass_dict = json.loads(result)

        assert "relevantDate" not in pass_dict
        assert "expirationDate" not in pass_dict


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


class TestResolvePriceFromTicket:
    """Tests for _resolve_price with actual price paid resolution."""

    def test_uses_tier_price_for_fixed_price_ticket(
        self,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should use tier.price when no price_paid or payment exists."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.ticket_price == "EUR 25.00"

    def test_uses_price_paid_for_offline_pwyc(
        self,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should use ticket.price_paid when set (offline PWYC)."""
        wallet_ticket.price_paid = Decimal("15.00")
        wallet_ticket.save()

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.ticket_price == "EUR 15.00"

    def test_uses_payment_amount_for_online_payment(
        self,
        wallet_ticket: Ticket,
        member_user: RevelUser,
        mock_signer: MagicMock,
    ) -> None:
        """Should use payment.amount for online Stripe payments."""
        Payment.objects.create(
            ticket=wallet_ticket,
            user=member_user,
            stripe_session_id="cs_test_123",
            status=Payment.PaymentStatus.SUCCEEDED,
            amount=Decimal("42.00"),
            platform_fee=Decimal("2.10"),
            currency="EUR",
        )
        # Refresh to load the payment relation
        wallet_ticket = Ticket.objects.full().get(pk=wallet_ticket.pk)

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.ticket_price == "EUR 42.00"

    def test_price_paid_takes_precedence_over_payment(
        self,
        wallet_ticket: Ticket,
        member_user: RevelUser,
        mock_signer: MagicMock,
    ) -> None:
        """price_paid should take precedence over payment.amount."""
        wallet_ticket.price_paid = Decimal("20.00")
        wallet_ticket.save()
        Payment.objects.create(
            ticket=wallet_ticket,
            user=member_user,
            stripe_session_id="cs_test_456",
            status=Payment.PaymentStatus.SUCCEEDED,
            amount=Decimal("99.00"),
            platform_fee=Decimal("4.95"),
            currency="EUR",
        )
        wallet_ticket = Ticket.objects.full().get(pk=wallet_ticket.pk)

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.ticket_price == "EUR 20.00"

    def test_free_ticket_shows_free(
        self,
        wallet_free_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should show 'Free' for zero price tickets."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_free_ticket)

        assert data.ticket_price == "Free"

    def test_uses_payment_currency(
        self,
        wallet_ticket: Ticket,
        member_user: RevelUser,
        mock_signer: MagicMock,
    ) -> None:
        """Should use the payment's currency for online payments."""
        Payment.objects.create(
            ticket=wallet_ticket,
            user=member_user,
            stripe_session_id="cs_test_789",
            status=Payment.PaymentStatus.SUCCEEDED,
            amount=Decimal("30.00"),
            platform_fee=Decimal("1.50"),
            currency="USD",
        )
        wallet_ticket = Ticket.objects.full().get(pk=wallet_ticket.pk)

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.ticket_price == "USD 30.00"


class TestVenueAddressResolution:
    """Tests for venue address resolution in pass data."""

    def test_uses_venue_address_from_tier(
        self,
        event_with_address: Event,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should use tier's venue address when available."""
        venue = Venue.objects.create(
            organization=event_with_address.organization,
            name="Tier Venue",
            address="456 Venue Street",
        )
        wallet_ticket.tier.venue = venue
        wallet_ticket.tier.save()
        wallet_ticket = Ticket.objects.full().get(pk=wallet_ticket.pk)

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.venue_name == "Tier Venue"
        assert data.address == "456 Venue Street"

    def test_uses_venue_address_from_ticket(
        self,
        event_with_address: Event,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should use ticket's venue address when tier has no venue."""
        venue = Venue.objects.create(
            organization=event_with_address.organization,
            name="Ticket Venue",
            address="789 Ticket Ave",
        )
        wallet_ticket.venue = venue
        wallet_ticket.save()
        wallet_ticket = Ticket.objects.full().get(pk=wallet_ticket.pk)

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.venue_name == "Ticket Venue"
        assert data.address == "789 Ticket Ave"

    def test_falls_back_to_event_address(
        self,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should fall back to event.address when no venue has an address."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.address == "123 Test Street, Test City"

    def test_venue_without_address_falls_back_to_event(
        self,
        event_with_address: Event,
        wallet_ticket: Ticket,
        mock_signer: MagicMock,
    ) -> None:
        """Should fall back to event.address when venue has no address."""
        venue = Venue.objects.create(
            organization=event_with_address.organization,
            name="No Address Venue",
        )
        wallet_ticket.tier.venue = venue
        wallet_ticket.tier.save()
        wallet_ticket = Ticket.objects.full().get(pk=wallet_ticket.pk)

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_pass_data(wallet_ticket)

        assert data.venue_name == "No Address Venue"
        # Venue has no address, so falls back to event.address
        assert data.address == event_with_address.address


class TestPassJsonFieldLayout:
    """Tests for field layout in pass JSON (header, primary, secondary)."""

    def _build_and_parse(self, generator: ApplePassGenerator, **overrides: t.Any) -> dict[str, t.Any]:
        now = timezone.now()
        colors = PassColors(background="rgb(0,0,0)", foreground="rgb(255,255,255)", label="rgb(128,128,128)")
        defaults: dict[str, t.Any] = {
            "serial_number": "serial",
            "description": "Test",
            "organization_name": "Test Org",
            "event_name": "Test Event",
            "event_start": now,
            "event_end": now + timedelta(hours=1),
            "address": None,
            "ticket_tier": "Tier",
            "ticket_price": "Free",
            "colors": colors,
            "logo_image": b"logo",
        }
        defaults.update(overrides)
        data = PassData(**defaults)
        result: dict[str, t.Any] = json.loads(generator._build_pass_json(data))
        return result

    def test_header_and_primary(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """Header has date only; primary uses org name as label."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"
        generator = ApplePassGenerator(signer=mock_signer)

        pass_dict = self._build_and_parse(generator)
        header_fields = pass_dict["eventTicket"]["headerFields"]

        assert len(header_fields) == 1
        assert header_fields[0]["key"] == "date"

        primary = pass_dict["eventTicket"]["primaryFields"][0]
        assert primary["label"] == "Test Org"
        assert primary["value"] == "Test Event"

    def test_venue_name_as_label_address_as_value(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """When venue and address both exist, venue name is the label and address is the value."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"
        generator = ApplePassGenerator(signer=mock_signer)

        pass_dict = self._build_and_parse(generator, venue_name="Yoga Bar", address="Stühmeyerstr. 33, Bochum")
        secondary = pass_dict["eventTicket"]["secondaryFields"]

        assert secondary[0]["key"] == "venue"
        assert secondary[0]["label"] == "YOGA BAR"
        assert secondary[0]["value"] == "Stühmeyerstr. 33, Bochum"

    def test_address_without_venue(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """When no venue but address exists, address is the main secondary field."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"
        generator = ApplePassGenerator(signer=mock_signer)

        pass_dict = self._build_and_parse(generator, address="123 Event Street")
        secondary = pass_dict["eventTicket"]["secondaryFields"]

        assert secondary[0]["key"] == "address"
        assert secondary[0]["value"] == "123 Event Street"

    def test_no_address_no_venue(self, settings: t.Any, mock_signer: MagicMock) -> None:
        """When neither venue nor address exist, secondary has no location fields."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"
        generator = ApplePassGenerator(signer=mock_signer)

        pass_dict = self._build_and_parse(generator)
        secondary = pass_dict["eventTicket"]["secondaryFields"]

        assert all(f["key"] not in ("venue", "address") for f in secondary)
