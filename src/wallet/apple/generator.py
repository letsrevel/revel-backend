"""Apple Wallet pass generator.

This module generates .pkpass files for event tickets. A .pkpass file is
a ZIP archive containing:
- pass.json: The pass definition
- manifest.json: SHA-1 hashes of all files
- signature: PKCS#7 signature of the manifest
- Images: icon, logo, thumbnail, etc.
"""

import colorsys
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from django.conf import settings
from django.utils import timezone
from PIL import Image, ImageDraw

from events.models import Ticket
from wallet.apple.signer import ApplePassSigner, ApplePassSignerError

logger = structlog.get_logger(__name__)


# Pass icon sizes (Apple requirements)
ICON_SIZES = {
    "icon.png": (29, 29),
    "icon@2x.png": (58, 58),
    "icon@3x.png": (87, 87),
}

LOGO_SIZES = {
    "logo.png": (160, 50),
    "logo@2x.png": (320, 100),
    "logo@3x.png": (480, 150),
}

THUMBNAIL_SIZES = {
    "thumbnail.png": (90, 90),
    "thumbnail@2x.png": (180, 180),
    "thumbnail@3x.png": (270, 270),
}


@dataclass
class PassColors:
    """Colors for an Apple Wallet pass."""

    background: str  # RGB format: "rgb(r, g, b)"
    foreground: str
    label: str


@dataclass
class PassField:
    """A field to display on the pass."""

    key: str
    label: str
    value: str
    change_message: str | None = None
    text_alignment: str | None = None  # PKTextAlignmentLeft, Right, Center, Natural


@dataclass
class PassData:
    """Data structure for building a pass."""

    serial_number: str
    auth_token: str
    description: str
    organization_name: str
    event_name: str
    event_start: datetime
    event_end: datetime
    location: str | None
    ticket_tier: str
    ticket_price: str  # Formatted price string e.g. "EUR 25.00" or "Free"
    colors: PassColors
    logo_image: bytes  # Always provided (from org logo or generated)
    thumbnail_image: bytes | None = None
    barcode_message: str = ""
    relevant_date: datetime | None = None
    extra_fields: list[PassField] = field(default_factory=list)


class ApplePassGeneratorError(Exception):
    """Raised when pass generation fails."""

    pass


class ApplePassGenerator:
    """Generates Apple Wallet .pkpass files for event tickets."""

    CONTENT_TYPE = "application/vnd.apple.pkpass"
    FILE_EXTENSION = "pkpass"

    def __init__(self, signer: ApplePassSigner | None = None) -> None:
        """Initialize the generator.

        Args:
            signer: The signer to use for creating signatures.
                   If not provided, a default signer is created.
        """
        self.signer = signer or ApplePassSigner()
        self._pass_type_id = settings.APPLE_WALLET_PASS_TYPE_ID
        self._team_id = settings.APPLE_WALLET_TEAM_ID
        # Web service URL must be HTTPS for Apple Wallet
        # Only set if BASE_URL is HTTPS, otherwise pass updates won't work
        base_url = settings.BASE_URL
        if base_url.startswith("https://"):
            self._web_service_url: str | None = f"{base_url}/api/wallet"
        else:
            self._web_service_url = None

    def get_pass_content_type(self) -> str:
        """Get the MIME content type for Apple passes."""
        return self.CONTENT_TYPE

    def get_pass_file_extension(self) -> str:
        """Get the file extension for Apple passes."""
        return self.FILE_EXTENSION

    def generate_pass(self, ticket: Ticket, auth_token: str | None = None) -> bytes:
        """Generate a .pkpass file for a ticket.

        Args:
            ticket: The ticket to generate a pass for.
            auth_token: Authentication token for pass updates.
                       If not provided, generates a new one.

        Returns:
            The .pkpass file as bytes.

        Raises:
            ApplePassGeneratorError: If pass generation fails.
        """
        try:
            # Build pass data from ticket
            pass_data = self._build_pass_data(ticket, auth_token)

            # Generate all files for the pass
            files = self._generate_pass_files(pass_data)

            # Create manifest
            manifest = self.signer.create_manifest(files)
            files["manifest.json"] = manifest

            # Sign manifest
            signature = self.signer.sign_manifest(manifest)
            files["signature"] = signature

            # Package into .pkpass (ZIP)
            pkpass_bytes = self._create_pkpass_archive(files)

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

    def _build_pass_data(self, ticket: Ticket, auth_token: str | None) -> PassData:
        """Build PassData from a Ticket model.

        Args:
            ticket: The ticket to extract data from.
            auth_token: Pre-generated auth token, or None to generate.

        Returns:
            PassData with all information needed for the pass.
        """
        event = ticket.event
        org = event.organization

        # Get colors from organization/event UUIDs
        colors = self._generate_colors(org.id, event.id)

        # Resolve logo image
        logo_image = self._resolve_logo_image(ticket)

        # Build location string with proper formatting
        # Line 1: Venue name/address
        # Line 2: City, Country
        location = self._build_location_string(event)

        # Generate auth token if not provided
        if auth_token is None:
            from wallet.models import generate_auth_token

            auth_token = generate_auth_token()

        # Format price
        if ticket.tier:
            if ticket.tier.price == 0:
                ticket_price = "Free"
            else:
                currency = ticket.tier.currency.upper()
                price_value = ticket.tier.price
                ticket_price = f"{currency} {price_value:.2f}"
        else:
            ticket_price = "Free"

        return PassData(
            serial_number=str(ticket.id),
            auth_token=auth_token,
            description=f"Ticket for {event.name}",
            organization_name=org.name,
            event_name=event.name,
            event_start=event.start,
            event_end=event.end,
            location=location,
            ticket_tier=ticket.tier.name if ticket.tier else "General Admission",
            ticket_price=ticket_price,
            colors=colors,
            logo_image=logo_image,
            barcode_message=str(ticket.id),
            relevant_date=event.start,
        )

    def _resolve_logo_image(self, ticket: Ticket) -> bytes:
        """Resolve logo image with fallback chain.

        Uses cover_art since the wallet logo area is rectangular.
        Order: event.cover_art → series.cover_art → organization.cover_art → generated from org ID

        Args:
            ticket: The ticket to get logo for.

        Returns:
            Logo image bytes (always returns something - generates fallback if needed).
        """
        event = ticket.event

        # Try event cover_art
        if event.cover_art:
            try:
                event.cover_art.seek(0)
                cover_bytes: bytes = event.cover_art.read()
                return cover_bytes
            except Exception:
                pass

        # Try series cover_art
        if event.event_series and event.event_series.cover_art:
            try:
                event.event_series.cover_art.seek(0)
                series_cover_bytes: bytes = event.event_series.cover_art.read()
                return series_cover_bytes
            except Exception:
                pass

        # Try organization cover_art
        if event.organization.cover_art:
            try:
                event.organization.cover_art.seek(0)
                org_cover_bytes: bytes = event.organization.cover_art.read()
                return org_cover_bytes
            except Exception:
                pass

        # Generate fallback logo based on organization ID
        return self._generate_org_logo(event.organization)

    def _generate_org_logo(self, organization: Any) -> bytes:
        """Generate a logo image based on organization ID.

        Creates a visually distinctive logo using a hue derived from the
        organization's UUID. The logo displays the organization's initial(s)
        on a colored background.

        Args:
            organization: The organization model.

        Returns:
            PNG image bytes for the generated logo.
        """
        # Derive a hue from the organization UUID
        org_uuid = organization.id
        # Use first 8 bytes of UUID to get a consistent hue
        hue = (org_uuid.int % 360) / 360.0

        # Create a vibrant but not too saturated color
        # HSL: hue varies, saturation 60%, lightness 45%
        rgb = colorsys.hls_to_rgb(hue, 0.45, 0.60)
        bg_color = (int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))

        # Get organization initial(s)
        name = organization.name or "R"
        # Take first letter of first 1-2 words
        words = name.split()[:2]
        initials = "".join(word[0].upper() for word in words if word)
        if not initials:
            initials = "R"  # Revel fallback

        # Generate logo at the largest size, then resize for other sizes
        largest_size = LOGO_SIZES["logo@3x.png"]
        return self._generate_text_logo(largest_size, initials, bg_color)

    def _build_location_string(self, event: Any) -> str | None:
        """Build a formatted location string for the pass.

        Args:
            event: The event model with location info.

        Returns:
            Address string or None if no location info.
        """
        if event.address:
            return str(event.address)
        return None

    def _generate_colors(self, org_id: UUID, event_id: UUID) -> PassColors:
        """Generate colors matching the Revel dark theme.

        Uses the Revel frontend dark color palette (purple-based).

        Args:
            org_id: Organization UUID (unused, kept for API compatibility).
            event_id: Event UUID (unused, kept for API compatibility).

        Returns:
            PassColors with Revel dark theme colors.
        """
        # Suppress unused variable warnings - kept for future customization

        # Revel dark theme colors (from frontend app.css)
        # --background: 270 30% 8% (HSL) -> dark purple background
        # --card: 270 25% 12% -> slightly lighter card background
        # --foreground: 270 10% 95% -> light text
        # --muted-foreground: 270 10% 65% -> muted labels

        # Convert HSL to RGB for background (270, 30%, 8%)
        # HSL: H=270 (purple), S=30%, L=8%
        bg_rgb = colorsys.hls_to_rgb(270 / 360, 0.08, 0.30)
        bg_color = f"rgb({int(bg_rgb[0] * 255)}, {int(bg_rgb[1] * 255)}, {int(bg_rgb[2] * 255)})"

        # Foreground: light text (270, 10%, 95%)
        fg_rgb = colorsys.hls_to_rgb(270 / 360, 0.95, 0.10)
        fg_color = f"rgb({int(fg_rgb[0] * 255)}, {int(fg_rgb[1] * 255)}, {int(fg_rgb[2] * 255)})"

        # Label: muted foreground (270, 10%, 65%)
        label_rgb = colorsys.hls_to_rgb(270 / 360, 0.65, 0.10)
        label_color = f"rgb({int(label_rgb[0] * 255)}, {int(label_rgb[1] * 255)}, {int(label_rgb[2] * 255)})"

        return PassColors(background=bg_color, foreground=fg_color, label=label_color)

    def _generate_pass_files(self, pass_data: PassData) -> dict[str, bytes]:
        """Generate all files needed for a pass.

        Args:
            pass_data: The pass data to generate files from.

        Returns:
            Dictionary mapping filename to content bytes.
        """
        files: dict[str, bytes] = {}

        # Generate pass.json
        files["pass.json"] = self._generate_pass_json(pass_data)

        # Generate icon images (required)
        icon_color = self._parse_rgb_color(pass_data.colors.background)
        for filename, size in ICON_SIZES.items():
            files[filename] = self._generate_colored_icon(size, icon_color)

        # Generate logo images (always available - either from org logo or generated)
        for filename, size in LOGO_SIZES.items():
            files[filename] = self._resize_image(pass_data.logo_image, size)

        return files

    def _generate_pass_json(self, pass_data: PassData) -> bytes:
        """Generate the pass.json content.

        Args:
            pass_data: The pass data to serialize.

        Returns:
            pass.json content as bytes.
        """

        # Format dates for Apple's expected format (ISO 8601 with colon in timezone)
        def format_date(dt: datetime) -> str:
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            # strftime %z gives +0000, but Apple wants +00:00
            formatted = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
            # Insert colon in timezone offset: +0000 -> +00:00
            if len(formatted) >= 5 and formatted[-5] in ('+', '-'):
                formatted = formatted[:-2] + ":" + formatted[-2:]
            return formatted

        # Format date/time for header (compact: "Jan 3, 2025 19:00")
        def format_date_time_compact(dt: datetime) -> str:
            return dt.strftime("%b %-d, %Y %H:%M")

        # Format time for display (full format for back fields)
        def format_time_display(dt: datetime) -> str:
            return dt.strftime("%b %d, %Y %I:%M %p")

        # Build secondary fields (venue)
        secondary_fields: list[dict[str, Any]] = []
        if pass_data.location:
            secondary_fields.append(
                {
                    "key": "venue",
                    "label": "VENUE",
                    "value": pass_data.location,
                    "changeMessage": "Venue changed to %@",
                }
            )

        pass_json: dict[str, Any] = {
            "formatVersion": 1,
            "passTypeIdentifier": self._pass_type_id,
            "serialNumber": pass_data.serial_number,
            "teamIdentifier": self._team_id,
            "organizationName": pass_data.organization_name,
            "description": pass_data.description,
            "backgroundColor": pass_data.colors.background,
            "foregroundColor": pass_data.colors.foreground,
            "labelColor": pass_data.colors.label,
            # Barcode
            "barcodes": [
                {
                    "format": "PKBarcodeFormatQR",
                    "message": pass_data.barcode_message,
                    "messageEncoding": "iso-8859-1",
                }
            ],
            # Event ticket structure
            # Layout:
            # [Organizer Name]                    [Date]
            #                           Jan 3, 2025 19:00
            # EVENT
            # Name of the Event
            #
            # VENUE
            # Full address here
            #
            # TICKET                              PRICE
            # Tier Name                        EUR 25.00
            "eventTicket": {
                "headerFields": [
                    {
                        "key": "organization",
                        "value": pass_data.organization_name,
                    },
                    {
                        "key": "date",
                        "label": "DATE",
                        "value": format_date_time_compact(pass_data.event_start),
                        "textAlignment": "PKTextAlignmentRight",
                        "changeMessage": "Event date changed to %@",
                    },
                ],
                "primaryFields": [
                    {
                        "key": "event",
                        "label": "EVENT",
                        "value": pass_data.event_name,
                    }
                ],
                "secondaryFields": secondary_fields,
                "auxiliaryFields": [
                    {
                        "key": "tier",
                        "label": "TICKET",
                        "value": pass_data.ticket_tier,
                    },
                    {
                        "key": "price",
                        "label": "PRICE",
                        "value": pass_data.ticket_price,
                        "textAlignment": "PKTextAlignmentRight",
                    },
                ],
                "backFields": [
                    {
                        "key": "ticket_id",
                        "label": "Ticket ID",
                        "value": pass_data.serial_number,
                    },
                    {
                        "key": "event_details",
                        "label": "Event Details",
                        "value": f"{pass_data.event_name}\n\n"
                        f"Start: {format_time_display(pass_data.event_start)}\n"
                        f"End: {format_time_display(pass_data.event_end)}",
                    },
                ],
            },
        }

        # Add full location to back fields if available
        if pass_data.location:
            pass_json["eventTicket"]["backFields"].append(
                {
                    "key": "full_location",
                    "label": "Full Address",
                    "value": pass_data.location,
                }
            )

        # Add relevant date for lock screen display
        if pass_data.relevant_date:
            pass_json["relevantDate"] = format_date(pass_data.relevant_date)

        # Add web service URL for push notifications (only if HTTPS)
        if self._web_service_url:
            pass_json["webServiceURL"] = self._web_service_url
            pass_json["authenticationToken"] = pass_data.auth_token

        return json.dumps(pass_json, indent=2).encode("utf-8")

    def _parse_rgb_color(self, rgb_string: str) -> tuple[int, int, int]:
        """Parse an RGB color string to a tuple.

        Args:
            rgb_string: Color in format "rgb(r, g, b)".

        Returns:
            Tuple of (r, g, b) integers.
        """
        # Extract numbers from "rgb(r, g, b)"
        import re

        match = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", rgb_string)
        if match:
            return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return (50, 50, 100)  # Default dark blue

    def _generate_colored_icon(self, size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
        """Generate a simple colored square icon.

        Args:
            size: (width, height) tuple.
            color: (r, g, b) tuple.

        Returns:
            PNG image as bytes.
        """
        img = Image.new("RGB", size, color)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    def _generate_text_logo(
        self,
        size: tuple[int, int],
        text: str,
        bg_color: tuple[int, int, int],
    ) -> bytes:
        """Generate a logo with text (e.g., organization initial).

        Args:
            size: (width, height) tuple.
            text: Text to display (usually 1-2 characters).
            bg_color: Background color as (r, g, b).

        Returns:
            PNG image as bytes.
        """
        from PIL import ImageFont

        # Create image with transparent background for logo
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Draw a rounded rectangle background
        margin = min(size) // 10
        draw.rounded_rectangle(
            [margin, margin, size[0] - margin, size[1] - margin],
            radius=min(size) // 5,
            fill=bg_color + (255,),  # Add alpha
        )

        # Use a larger font for the text
        # Target font size is about 40% of the smaller dimension
        font_size = int(min(size) * 0.4)
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont
        try:
            # Try to load a system font
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (OSError, IOError):
            try:
                # Fallback for Linux
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
            except (OSError, IOError):
                # Last resort: use default font (will be small)
                font = ImageFont.load_default()

        # Draw text centered
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        text_x = (size[0] - text_width) // 2
        text_y = (size[1] - text_height) // 2
        draw.text((text_x, text_y), text, fill=(255, 255, 255, 255), font=font)

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    def _resize_image(self, image_data: bytes, size: tuple[int, int]) -> bytes:
        """Resize an image to the specified size.

        Args:
            image_data: Original image as bytes.
            size: Target (width, height).

        Returns:
            Resized PNG image as bytes.
        """
        try:
            img: Image.Image = Image.open(io.BytesIO(image_data))
            # Use LANCZOS for high-quality downsampling
            img = img.resize(size, Image.Resampling.LANCZOS)
            # Convert to RGB if necessary (for PNG output)
            if img.mode in ("RGBA", "P"):
                # Keep transparency for logos
                pass
            elif img.mode != "RGB":
                img = img.convert("RGB")

            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception as e:
            logger.warning("image_resize_failed", error=str(e))
            # Return a placeholder on error
            return self._generate_colored_icon(size, (100, 100, 100))

    def _create_pkpass_archive(self, files: dict[str, bytes]) -> bytes:
        """Create the .pkpass ZIP archive.

        Args:
            files: Dictionary mapping filename to content.

        Returns:
            ZIP archive as bytes.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, content in files.items():
                zf.writestr(filename, content)
        return buffer.getvalue()
