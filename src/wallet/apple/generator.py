"""Apple Wallet pass generator.

This module generates .pkpass files for event tickets. A .pkpass file is
a ZIP archive containing:
- pass.json: The pass definition
- manifest.json: SHA-1 hashes of all files
- signature: PKCS#7 signature of the manifest
- Images: icon, logo, etc.
"""

import io
import json
import typing as t
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import structlog
from django.conf import settings

from events.models import Ticket
from wallet.apple.formatting import (
    PassColors,
    format_date_compact,
    format_date_full,
    format_iso_date,
    format_price,
    get_theme_colors,
)
from wallet.apple.images import (
    ICON_SIZES,
    LOGO_SIZES,
    generate_colored_icon,
    generate_fallback_logo,
    parse_rgb_color,
    resize_image,
    resolve_cover_art,
)
from wallet.apple.signer import ApplePassSigner, ApplePassSignerError

logger = structlog.get_logger(__name__)


@dataclass
class PassData:
    """Data structure for building a pass."""

    serial_number: str
    description: str
    organization_name: str
    event_name: str
    event_start: datetime
    event_end: datetime
    address: str | None  # Full address for back of pass
    ticket_tier: str
    ticket_price: str
    colors: PassColors
    logo_image: bytes
    barcode_message: str = ""
    relevant_date: datetime | None = None
    guest_name: str | None = None
    venue_name: str | None = None  # Venue name for front of pass
    sector_name: str | None = None
    seat_label: str | None = None


class ApplePassGeneratorError(Exception):
    """Raised when pass generation fails."""


class ApplePassGenerator:
    """Generates Apple Wallet .pkpass files for event tickets."""

    CONTENT_TYPE = "application/vnd.apple.pkpass"
    FILE_EXTENSION = "pkpass"

    def __init__(self, signer: ApplePassSigner | None = None) -> None:
        """Initialize the generator.

        Args:
            signer: The signer to use. If not provided, a default is created.
        """
        self.signer = signer or ApplePassSigner()
        self._pass_type_id = settings.APPLE_WALLET_PASS_TYPE_ID
        self._team_id = settings.APPLE_WALLET_TEAM_ID

    def generate_pass(self, ticket: Ticket) -> bytes:
        """Generate a .pkpass file for a ticket.

        Args:
            ticket: The ticket to generate a pass for.

        Returns:
            The .pkpass file as bytes.

        Raises:
            ApplePassGeneratorError: If pass generation fails.
        """
        try:
            pass_data = self._build_pass_data(ticket)
            files = self._generate_files(pass_data)

            # Create manifest and sign
            manifest = self.signer.create_manifest(files)
            files["manifest.json"] = manifest
            files["signature"] = self.signer.sign_manifest(manifest)

            pkpass_bytes = self._create_archive(files)

            logger.info(
                "pass_generated",
                ticket_id=str(ticket.id),
                event_id=str(ticket.event_id),
                size=len(pkpass_bytes),
            )

            return pkpass_bytes

        except ApplePassSignerError:
            raise
        except Exception as e:
            logger.error("pass_generation_failed", ticket_id=str(ticket.id), error=str(e))
            raise ApplePassGeneratorError(f"Failed to generate pass: {e}")

    def _build_pass_data(self, ticket: Ticket) -> PassData:
        """Build PassData from a Ticket model."""
        event = ticket.event
        org = event.organization

        # Resolve logo image (cover_art with fallback to generated)
        logo_image = resolve_cover_art(event) or generate_fallback_logo(org)

        # Resolve actual price paid:
        # 1. ticket.price_paid (offline/at_the_door PWYC)
        # 2. ticket.payment.amount (online Stripe payment)
        # 3. tier.price (fixed-price fallback)
        price, currency = self._resolve_price(ticket)
        ticket_price = format_price(price, currency) if price > 0 else "Free"

        # Extract venue (from tier's venue, ticket's venue, or event's venue)
        venue = None
        if ticket.tier and ticket.tier.venue:
            venue = ticket.tier.venue
        elif ticket.venue:
            venue = ticket.venue
        elif event.venue:
            venue = event.venue
        venue_name = venue.name if venue else None

        # Extract sector name (from tier's sector or ticket's sector)
        sector_name: str | None = None
        if ticket.tier and ticket.tier.sector:
            sector_name = ticket.tier.sector.name
        elif ticket.sector:
            sector_name = ticket.sector.name

        # Extract seat label
        seat_label = ticket.seat.label if ticket.seat else None

        return PassData(
            serial_number=str(ticket.id),
            description=f"Ticket for {event.name}",
            organization_name=org.name,
            event_name=event.name,
            event_start=event.start,
            event_end=event.end,
            address=(venue.full_address() if venue else None) or event.address or None,
            ticket_tier=ticket.tier.name if ticket.tier else "General Admission",
            ticket_price=ticket_price,
            colors=get_theme_colors(),
            logo_image=logo_image,
            barcode_message=str(ticket.id),
            relevant_date=event.start,
            guest_name=ticket.guest_name,
            venue_name=venue_name,
            sector_name=sector_name,
            seat_label=seat_label,
        )

    @staticmethod
    def _resolve_price(ticket: Ticket) -> tuple[Decimal, str]:
        """Resolve the actual price paid for a ticket.

        Priority:
        1. ticket.price_paid — explicitly recorded for offline/at_the_door PWYC
        2. ticket.payment.amount — online Stripe payment amount
        3. tier.price — fixed-price fallback

        Returns:
            Tuple of (price, currency).
        """
        tier = ticket.tier
        currency = tier.currency if tier else "EUR"

        if ticket.price_paid is not None:
            return ticket.price_paid, currency

        try:
            payment = ticket.payment
            return payment.amount, payment.currency
        except Ticket.payment.RelatedObjectDoesNotExist:
            pass

        return (tier.price if tier else Decimal(0)), currency

    def _generate_files(self, pass_data: PassData) -> dict[str, bytes]:
        """Generate all files needed for a pass."""
        files: dict[str, bytes] = {}

        # pass.json
        files["pass.json"] = self._build_pass_json(pass_data)

        # Icons
        icon_color = parse_rgb_color(pass_data.colors.background)
        for filename, size in ICON_SIZES.items():
            files[filename] = generate_colored_icon(size, icon_color)

        # Logos
        for filename, size in LOGO_SIZES.items():
            files[filename] = resize_image(pass_data.logo_image, size)

        return files

    def _build_pass_json(self, data: PassData) -> bytes:
        """Build the pass.json content.

        Apple Wallet eventTicket layout:
        - headerFields: Top row (date only — org name via organizationName)
        - primaryFields: Main content (event name)
        - secondaryFields: First info row (venue, section, seat)
        - auxiliaryFields: Second info row (ticket tier, price, guest name)
        - backFields: Back of pass (full details)

        Guest name is placed at the end of auxiliaryFields to allow it to
        use remaining space without being squeezed between other fields.
        """
        # Build secondary fields: venue > section > seat (left to right)
        secondary_fields: list[dict[str, t.Any]] = []

        if data.venue_name:
            secondary_fields.append(
                {
                    "key": "venue",
                    "label": "VENUE",
                    "value": data.venue_name,
                }
            )

        if data.sector_name:
            secondary_fields.append(
                {
                    "key": "sector",
                    "label": "SECTION",
                    "value": data.sector_name,
                }
            )

        if data.seat_label:
            secondary_fields.append(
                {
                    "key": "seat",
                    "label": "SEAT",
                    "value": data.seat_label,
                    "textAlignment": "PKTextAlignmentRight",
                }
            )

        # Build auxiliary fields: ticket tier, price, then guest name (if present)
        # Guest name is last so it can use more horizontal space for long names
        auxiliary_fields: list[dict[str, t.Any]] = [
            {"key": "tier", "label": "TICKET", "value": data.ticket_tier},
            {
                "key": "price",
                "label": "PRICE",
                "value": data.ticket_price,
            },
        ]

        # Add guest name at the end - right-aligned so it flows naturally
        if data.guest_name:
            auxiliary_fields.append(
                {
                    "key": "guest",
                    "label": "GUEST",
                    "value": data.guest_name,
                    "textAlignment": "PKTextAlignmentRight",
                }
            )

        pass_dict: dict[str, t.Any] = {
            "formatVersion": 1,
            "passTypeIdentifier": self._pass_type_id,
            "serialNumber": data.serial_number,
            "teamIdentifier": self._team_id,
            "organizationName": data.organization_name,
            "description": data.description,
            "backgroundColor": data.colors.background,
            "foregroundColor": data.colors.foreground,
            "labelColor": data.colors.label,
            "barcodes": [
                {
                    "format": "PKBarcodeFormatQR",
                    "message": data.barcode_message,
                    "messageEncoding": "iso-8859-1",
                }
            ],
            "eventTicket": {
                "headerFields": [
                    {
                        "key": "date",
                        "value": format_date_compact(data.event_start),
                    },
                ],
                "primaryFields": [
                    {"key": "event", "label": "EVENT", "value": data.event_name},
                ],
                "secondaryFields": secondary_fields,
                "auxiliaryFields": auxiliary_fields,
                "backFields": self._build_back_fields(data),
            },
        }

        # Add relevant date for lock screen
        if data.relevant_date:
            pass_dict["relevantDate"] = format_iso_date(data.relevant_date)

        return json.dumps(pass_dict, indent=2).encode("utf-8")

    def _build_back_fields(self, data: PassData) -> list[dict[str, str]]:
        """Build the back fields for the pass."""
        fields: list[dict[str, str]] = [
            {"key": "ticket_id", "label": "Ticket ID", "value": data.serial_number},
        ]

        # Add guest name
        if data.guest_name:
            fields.append({"key": "guest_name", "label": "Guest", "value": data.guest_name})

        fields.append(
            {
                "key": "event_details",
                "label": "Event Details",
                "value": (
                    f"{data.event_name}\n\n"
                    f"Start: {format_date_full(data.event_start)}\n"
                    f"End: {format_date_full(data.event_end)}"
                ),
            }
        )

        # Add venue and address
        if data.venue_name:
            fields.append(
                {
                    "key": "venue_name",
                    "label": "Venue",
                    "value": data.venue_name,
                }
            )

        if data.address:
            fields.append(
                {
                    "key": "full_address",
                    "label": "Address",
                    "value": data.address,
                }
            )

        # Add seating info
        if data.sector_name or data.seat_label:
            seating_parts = []
            if data.sector_name:
                seating_parts.append(f"Section: {data.sector_name}")
            if data.seat_label:
                seating_parts.append(f"Seat: {data.seat_label}")
            fields.append(
                {
                    "key": "seating",
                    "label": "Seating",
                    "value": "\n".join(seating_parts),
                }
            )

        return fields

    def _create_archive(self, files: dict[str, bytes]) -> bytes:
        """Create the .pkpass ZIP archive."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, content in files.items():
                zf.writestr(filename, content)
        return buffer.getvalue()
