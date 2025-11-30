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
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any

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
    venue: str | None
    ticket_tier: str
    ticket_price: str
    colors: PassColors
    logo_image: bytes
    barcode_message: str = ""
    relevant_date: datetime | None = None


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

        # Format ticket price
        if ticket.tier and ticket.tier.price > 0:
            ticket_price = format_price(ticket.tier.price, ticket.tier.currency)
        else:
            ticket_price = "Free"

        return PassData(
            serial_number=str(ticket.id),
            description=f"Ticket for {event.name}",
            organization_name=org.name,
            event_name=event.name,
            event_start=event.start,
            event_end=event.end,
            venue=event.address or None,
            ticket_tier=ticket.tier.name if ticket.tier else "General Admission",
            ticket_price=ticket_price,
            colors=get_theme_colors(),
            logo_image=logo_image,
            barcode_message=str(ticket.id),
            relevant_date=event.start,
        )

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
        """Build the pass.json content."""
        # Build secondary fields (venue)
        secondary_fields: list[dict[str, Any]] = []
        if data.venue:
            secondary_fields.append(
                {
                    "key": "venue",
                    "label": "VENUE",
                    "value": data.venue,
                }
            )

        pass_dict: dict[str, Any] = {
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
                    {"key": "organization", "value": data.organization_name},
                    {
                        "key": "date",
                        "label": "DATE",
                        "value": format_date_compact(data.event_start),
                        "textAlignment": "PKTextAlignmentRight",
                    },
                ],
                "primaryFields": [
                    {"key": "event", "label": "EVENT", "value": data.event_name},
                ],
                "secondaryFields": secondary_fields,
                "auxiliaryFields": [
                    {"key": "tier", "label": "TICKET", "value": data.ticket_tier},
                    {
                        "key": "price",
                        "label": "PRICE",
                        "value": data.ticket_price,
                        "textAlignment": "PKTextAlignmentRight",
                    },
                ],
                "backFields": self._build_back_fields(data),
            },
        }

        # Add relevant date for lock screen
        if data.relevant_date:
            pass_dict["relevantDate"] = format_iso_date(data.relevant_date)

        return json.dumps(pass_dict, indent=2).encode("utf-8")

    def _build_back_fields(self, data: PassData) -> list[dict[str, str]]:
        """Build the back fields for the pass."""
        fields = [
            {"key": "ticket_id", "label": "Ticket ID", "value": data.serial_number},
            {
                "key": "event_details",
                "label": "Event Details",
                "value": (
                    f"{data.event_name}\n\n"
                    f"Start: {format_date_full(data.event_start)}\n"
                    f"End: {format_date_full(data.event_end)}"
                ),
            },
        ]

        if data.venue:
            fields.append(
                {
                    "key": "full_location",
                    "label": "Full Address",
                    "value": data.venue,
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
