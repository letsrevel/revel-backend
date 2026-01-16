import base64
import mimetypes
import typing as t
from io import BytesIO

import qrcode
import structlog
from django.template import Context, Template
from django.template.loader import render_to_string
from weasyprint import HTML

from accounts.models import RevelUser
from events import models

from .models import Ticket

logger = structlog.get_logger(__name__)


def get_invitation_message(user: RevelUser, event: models.Event) -> str:
    """Get invitation message.

    If the event has a custom invitation message, render it as a Django template.
    Otherwise, use the default template.
    """
    context = {"user": user, "event": event}

    if event.invitation_message:
        template = Template(event.invitation_message)
        return template.render(Context(context))

    return render_to_string("events/default_invitation_message.txt", context=context)


def _uuid_to_color(uuid_value: str) -> tuple[str, str]:
    """Convert a UUID to a consistent HSL color palette.

    Args:
        uuid_value: UUID string to convert

    Returns:
        Tuple of (primary_color, secondary_color) as HSL strings
    """
    # Use first 8 chars of UUID to generate a hue (0-360)
    hash_value = int(uuid_value.replace("-", "")[:8], 16)
    hue = hash_value % 360

    # Primary color: vibrant
    primary = f"hsl({hue}, 65%, 55%)"

    # Secondary color: shifted hue, slightly lighter
    secondary_hue = (hue + 30) % 360
    secondary = f"hsl({secondary_hue}, 60%, 60%)"

    return primary, secondary


def _get_logo_initials(name: str) -> str:
    """Get initials from a name for logo fallback.

    Args:
        name: Name to extract initials from

    Returns:
        Up to 2 uppercase initials
    """
    words = name.strip().split()
    if len(words) >= 2:
        return f"{words[0][0]}{words[1][0]}".upper()
    elif words:
        return words[0][:2].upper()
    return "??"


def _get_branding_assets(event: "models.Event") -> tuple[t.Any | None, t.Any | None, str]:
    """Get logo and cover_art with fallback priority: Event > EventSeries > Organization.

    Args:
        event: Event to get branding assets for

    Returns:
        Tuple of (logo_file, cover_art_file, branding_source_name)
    """
    logo_file = None
    cover_art_file = None
    branding_source_name = event.name

    # Try Event first
    if event.logo:
        logo_file = event.logo
    if event.cover_art:
        cover_art_file = event.cover_art

    # Try EventSeries if event doesn't have them
    if event.event_series:
        if not logo_file and event.event_series.logo:
            logo_file = event.event_series.logo
            branding_source_name = event.event_series.name
        if not cover_art_file and event.event_series.cover_art:
            cover_art_file = event.event_series.cover_art

    # Try Organization as final fallback
    if not logo_file and event.organization.logo:
        logo_file = event.organization.logo
        branding_source_name = event.organization.name
    if not cover_art_file and event.organization.cover_art:
        cover_art_file = event.organization.cover_art

    return logo_file, cover_art_file, branding_source_name


def _file_to_data_uri(file_field: t.Any) -> str | None:
    """Convert a Django FileField/ImageField to a base64 data URI.

    Args:
        file_field: Django file field to convert

    Returns:
        Data URI string or None if conversion fails
    """
    if not file_field:
        return None

    try:
        file_field.open("rb")
        file_data = file_field.read()
        file_field.close()

        # Detect MIME type from file extension
        mime_type, _ = mimetypes.guess_type(file_field.name)
        if not mime_type:
            mime_type = "image/jpeg"  # Default fallback

        file_base64 = base64.b64encode(file_data).decode("utf-8")
        return f"data:{mime_type};base64,{file_base64}"
    except Exception:
        logger.debug("file_to_data_uri_failed", file_name=getattr(file_field, "name", None))
        return None


def create_ticket_pdf(ticket: Ticket) -> bytes:
    """Generates a PDF version of a ticket using weasyprint.

    Args:
        ticket: The Ticket object, expected to have related event, user, tier, etc., prefetched.

    Returns:
        The PDF content as bytes.
    """
    event = ticket.event

    # Generate QR Code from the ticket's UUID
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(str(ticket.id))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffered = BytesIO()
    img.save(buffered, "PNG")
    qr_code_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    # Get branding assets with fallback priority: Event > EventSeries > Organization
    logo_file, cover_art_file, branding_source_name = _get_branding_assets(event)

    # Convert images to base64 data URIs for WeasyPrint
    logo_data_uri = _file_to_data_uri(logo_file)
    cover_art_data_uri = _file_to_data_uri(cover_art_file)

    # Generate color scheme from UUID
    color_source_id = str(event.id)
    if logo_file and event.event_series and event.event_series.logo:
        color_source_id = str(event.event_series.id)
    elif logo_file and event.organization.logo:
        color_source_id = str(event.organization.id)

    primary_color, secondary_color = _uuid_to_color(color_source_id)
    logo_initials = _get_logo_initials(branding_source_name)

    # Prepare context for the HTML template
    context_data = {
        "event_name": event.name,
        "organization_name": event.organization.name,
        "user_display_name": ticket.user.get_display_name(),
        "guest_name": ticket.guest_name,
        "tier_name": ticket.tier.name,
        "start_datetime": event.start.strftime("%A, %B %d, %Y at %I:%M %p %Z"),
        "address": event.full_address(),
        "qr_code_base64": qr_code_base64,
        "ticket_id": str(ticket.id),
        "ticket_id_short": str(ticket.id)[:8].upper(),
        "logo_url": logo_data_uri,
        "cover_art_url": cover_art_data_uri,
        "logo_initials": logo_initials,
        "primary_color": primary_color,
        "secondary_color": secondary_color,
        # Venue/seating info
        "venue_name": ticket.venue.name if ticket.venue else None,
        "sector_name": ticket.sector.name if ticket.sector else None,
        "seat_label": ticket.seat.label if ticket.seat else None,
        "seat_row": ticket.seat.row if ticket.seat else None,
        "seat_number": ticket.seat.number if ticket.seat else None,
    }

    # Render and generate PDF
    html_string = render_to_string("events/ticket.html", context=context_data)
    html = HTML(string=html_string)
    return t.cast(bytes, html.write_pdf())
