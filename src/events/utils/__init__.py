"""Pure utilities for the events app.

Helpers in this package may be imported by both models and services — unlike
the service layer, they must not themselves import from ``events.service``.
Model imports are deferred to avoid circular-import issues when submodules
(e.g. ``recurrence_validators``) are pulled in during model loading.
"""

import base64
import mimetypes
import typing as t
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import structlog
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateformat import format as date_format

if t.TYPE_CHECKING:
    from accounts.models import RevelUser
    from events import models
    from events.models import HeldSeriesPass, Organization, Ticket

logger = structlog.get_logger(__name__)

# Default date format for user-facing dates: "Friday, February 6, 2026 at 7:00 PM CET"
DEFAULT_DATE_FORMAT = "l, F j, Y \\a\\t g:i A T"


def get_user_timezone(user: "RevelUser") -> ZoneInfo | None:
    """Resolve the user's preferred timezone via ``general_preferences.city``.

    Returns ``None`` when the user has no preferences row, no city, an empty
    timezone, or an unrecognized timezone string. Callers should fall back to
    the event's timezone (or UTC) when this returns ``None``.

    Args:
        user: The user whose preferences to inspect.

    Returns:
        A ``ZoneInfo`` for the user's city timezone, or ``None`` when
        unresolved.
    """
    try:
        prefs = user.general_preferences
    except Exception:
        return None
    if prefs is None or getattr(prefs, "city_id", None) is None:
        return None
    tz_name = prefs.city.timezone if prefs.city else None
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except (KeyError, ValueError):
        logger.warning("invalid_timezone_for_user", user_id=user.id, timezone=tz_name)
        return None


def get_event_timezone(event: "models.Event") -> ZoneInfo:
    """Get the timezone for an event based on its city.

    Falls back to UTC if no city or timezone is set.

    Args:
        event: Event instance

    Returns:
        ZoneInfo for the event's timezone
    """
    if event.city and event.city.timezone:
        try:
            return ZoneInfo(event.city.timezone)
        except KeyError:
            logger.warning(
                "invalid_timezone_for_city",
                city_id=event.city.id,
                timezone=event.city.timezone,
            )
    return ZoneInfo("UTC")


def get_organization_timezone(org: "Organization") -> ZoneInfo:
    """Return the org's city timezone, falling back to the platform default.

    Args:
        org: Organization instance.

    Returns:
        A ``ZoneInfo`` for the org's city timezone, or the platform default
        (``settings.TIME_ZONE``) when no city or timezone is set.
    """
    if org.city and org.city.timezone:
        return ZoneInfo(org.city.timezone)
    return ZoneInfo(settings.TIME_ZONE)


def format_event_datetime(
    dt: datetime | None,
    event: "models.Event",
    fmt: str = DEFAULT_DATE_FORMAT,
) -> str:
    r"""Format a datetime in the event's timezone.

    Args:
        dt: Datetime to format (must be timezone-aware)
        event: Event to get timezone from
        fmt: Date format string (default: "l, F j, Y \a\t g:i A T")

    Returns:
        Formatted datetime string, or empty string if dt is None
    """
    if not dt:
        return ""

    event_tz = get_event_timezone(event)
    # Convert the datetime to the event's timezone
    dt_in_event_tz = dt.astimezone(event_tz)
    # Use timezone.override to ensure Django's date_format uses the correct timezone
    with timezone.override(event_tz):
        return date_format(dt_in_event_tz, fmt)


class _SafeAccessStr(str):
    """Empty string that silently absorbs attribute and item access.

    Used as the defaultdict factory for format_map so that unknown placeholders
    — including dotted ones like {user.email} or indexed ones like {user[0]} —
    always resolve to an empty string instead of raising AttributeError/KeyError.
    """

    def __getattr__(self, name: str) -> "_SafeAccessStr":
        """Return an empty safe string for any attribute access."""
        return _SafeAccessStr()

    def __getitem__(self, key: object) -> "_SafeAccessStr":
        """Return an empty safe string for any item access."""
        return _SafeAccessStr()


def get_invitation_message(display_name: str, event: "models.Event") -> str:
    """Get invitation message.

    If the event has a custom invitation message, render it using safe string
    interpolation with a curated allowlist of variables. This prevents SSTI by
    never executing event.invitation_message as a Django template.

    Supported placeholders: {user_name}, {event_name}, {organization_name}, {event_date}.
    Unknown placeholders (including dotted ones like {user.email}) resolve to an
    empty string and never leak sensitive data.

    Otherwise, use the default template.

    Args:
        display_name: The recipient's display name (user display name or email for pending invitations).
        event: The event the invitation is for.
    """
    if event.invitation_message:
        safe_context: dict[str, _SafeAccessStr] = {
            "user_name": _SafeAccessStr(display_name),
            "event_name": _SafeAccessStr(event.name),
            "organization_name": _SafeAccessStr(event.organization.name),
            "event_date": _SafeAccessStr(
                format_event_datetime(event.start, event, fmt="F j, Y") if event.start else ""
            ),
        }
        try:
            return event.invitation_message.format_map(defaultdict(_SafeAccessStr, safe_context))
        except (ValueError, AttributeError):
            logger.warning("invitation_message_format_error", event_id=str(event.id))
            return event.invitation_message

    context = {"display_name": display_name, "event": event}
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

    Prefers optimized variants (logo_thumbnail, cover_art_social) over full-resolution
    originals to reduce PDF file size, falling back to originals if thumbnails
    haven't been generated yet.

    Args:
        event: Event to get branding assets for

    Returns:
        Tuple of (logo_file, cover_art_file, branding_source_name)
    """
    logo_file = None
    cover_art_file = None
    branding_source_name = event.name

    # Try Event first (prefer optimized variants to reduce file size)
    if event.logo_thumbnail or event.logo:
        logo_file = event.logo_thumbnail or event.logo
    if event.cover_art_social or event.cover_art:
        cover_art_file = event.cover_art_social or event.cover_art

    # Try EventSeries if event doesn't have them
    if event.event_series:
        if not logo_file and (event.event_series.logo_thumbnail or event.event_series.logo):
            logo_file = event.event_series.logo_thumbnail or event.event_series.logo
            branding_source_name = event.event_series.name
        if not cover_art_file and (event.event_series.cover_art_social or event.event_series.cover_art):
            cover_art_file = event.event_series.cover_art_social or event.event_series.cover_art

    # Try Organization as final fallback
    if not logo_file and (event.organization.logo_thumbnail or event.organization.logo):
        logo_file = event.organization.logo_thumbnail or event.organization.logo
        branding_source_name = event.organization.name
    if not cover_art_file and (event.organization.cover_art_social or event.organization.cover_art):
        cover_art_file = event.organization.cover_art_social or event.organization.cover_art

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


def create_ticket_pdf(ticket: "Ticket") -> bytes:
    """Generates a PDF version of a ticket using weasyprint.

    Args:
        ticket: The Ticket object, expected to have related event, user, tier, etc., prefetched.

    Returns:
        The PDF content as bytes.
    """
    import qrcode
    from weasyprint import HTML

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
        "start_datetime": format_event_datetime(event.start, event),
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
        # Brand assets (absolute paths for WeasyPrint file:// resolution)
        "font_dir": str(settings.BASE_DIR / "fonts"),
        "brand_logo": str(settings.BASE_DIR / "assets" / "brand" / "revel-logo.png"),
    }

    # Render and generate PDF
    html_string = render_to_string("events/ticket.html", context=context_data)
    html = HTML(string=html_string)
    return t.cast(bytes, html.write_pdf())


def create_series_pass_pdf(held_pass: "HeldSeriesPass") -> bytes:
    """Generates a PDF version of a series pass using weasyprint.

    Args:
        held_pass: The HeldSeriesPass, expected to have ``series_pass`` (and its
            ``event_series``/``organization``) and covered-event tier links
            prefetched to avoid N+1 queries.

    Returns:
        The PDF content as bytes.
    """
    import qrcode
    from weasyprint import HTML

    series_pass = held_pass.series_pass
    event_series = series_pass.event_series
    organization = event_series.organization

    # QR payload matches the check-in resolution contract: "series:<held_pass.id>".
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(f"series:{held_pass.id}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffered = BytesIO()
    img.save(buffered, "PNG")
    qr_code_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    links = list(series_pass.tier_links.select_related("event").order_by("event__start"))
    covered_events = [
        {"name": link.event.name, "start": format_event_datetime(link.event.start, link.event)} for link in links
    ]

    # Branding: a series pass has no single "event" of its own, so reuse the
    # existing Event > EventSeries > Organization fallback keyed off the
    # earliest covered event (all covered events share the same series).
    if links:
        logo_file, cover_art_file, branding_source_name = _get_branding_assets(links[0].event)
    else:
        logo_file = (
            event_series.logo_thumbnail or event_series.logo or organization.logo_thumbnail or organization.logo
        )
        cover_art_file = (
            event_series.cover_art_social
            or event_series.cover_art
            or organization.cover_art_social
            or organization.cover_art
        )
        branding_source_name = event_series.name

    logo_data_uri = _file_to_data_uri(logo_file)
    cover_art_data_uri = _file_to_data_uri(cover_art_file)

    color_source_id = str(event_series.id) if (event_series.logo or event_series.cover_art) else str(organization.id)
    primary_color, secondary_color = _uuid_to_color(color_source_id)
    logo_initials = _get_logo_initials(branding_source_name)

    context_data = {
        "series_name": event_series.name,
        "pass_name": series_pass.name,
        "organization_name": organization.name,
        "user_display_name": held_pass.user.get_display_name(),
        "covered_events": covered_events,
        "qr_code_base64": qr_code_base64,
        "pass_id": str(held_pass.id),
        "pass_id_short": str(held_pass.id)[:8].upper(),
        "logo_url": logo_data_uri,
        "cover_art_url": cover_art_data_uri,
        "logo_initials": logo_initials,
        "primary_color": primary_color,
        "secondary_color": secondary_color,
        "font_dir": str(settings.BASE_DIR / "fonts"),
        "brand_logo": str(settings.BASE_DIR / "assets" / "brand" / "revel-logo.png"),
    }

    html_string = render_to_string("events/series_pass.html", context=context_data)
    html = HTML(string=html_string)
    return t.cast(bytes, html.write_pdf())
