"""Google Wallet pass payload builder.

Builds the ``eventTicketClasses`` / ``eventTicketObjects`` payload embedded in
a fat "save to Google Wallet" JWT. Unlike the Apple rail there is no file to
generate: Google creates the class and object server-side when the user opens
the signed save link.

See: https://developers.google.com/wallet/tickets/events
"""

import typing as t
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from events.models import Event, HeldSeriesPass, Organization, OrganizationMember, Ticket
from events.utils import get_event_timezone, get_organization_timezone
from wallet.apple.formatting import format_iso_date, format_price, get_theme_hex_background
from wallet.apple.generator import PASS_EXPIRATION_GRACE_PERIOD
from wallet.pricing import resolve_ticket_price


def _pass_id(kind: str, entity_id: t.Any) -> str:
    """Build a namespaced Google Wallet class/object ID."""
    return f"{settings.GOOGLE_WALLET_ISSUER_ID}.{settings.GOOGLE_WALLET_CLASS_PREFIX}.{kind}.{entity_id}"


def _localized(value: str) -> dict[str, t.Any]:
    """Wrap a string in Google's LocalizedString shape."""
    return {"defaultValue": {"language": "en", "value": value}}


def _org_logo_url(org: Organization) -> str | None:
    """Stable indirection URL for the org logo, or None when unset.

    Raw media URLs die when a logo is replaced (uploads delete the old file),
    which would void every save link already sitting in ticket emails. The
    API endpoint always serves the *current* logo or a placeholder.
    """
    if not (org.logo_thumbnail or org.logo):
        return None
    path = reverse("api:organization_logo", kwargs={"organization_id": org.id})
    return f"{settings.BASE_URL.rstrip('/')}{path}"


def _event_cover_url(event: Event) -> str | None:
    """Stable indirection URL for the event cover art, or None when unset."""
    if not event.cover_art:
        return None
    path = reverse("api:event_cover_art", kwargs={"event_id": event.id})
    return f"{settings.BASE_URL.rstrip('/')}{path}"


def _image(url: str | None) -> dict[str, t.Any] | None:
    """Wrap a URL in Google's Image shape.

    Google's servers fetch the image when the pass is saved and reject
    non-HTTPS URLs, which voids the entire save link (e.g. local dev's
    http://localhost media URLs) — omit the image instead.
    """
    if not url or not url.startswith("https://"):
        return None
    return {"sourceUri": {"uri": url}}


def _build_class(
    class_id: str,
    issuer_name: str,
    event_name: str,
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    venue_name: str | None,
    address: str | None,
    logo_url: str | None,
    hero_url: str | None,
) -> dict[str, t.Any]:
    """Build an EventTicketClass dict."""
    cls: dict[str, t.Any] = {
        "id": class_id,
        "issuerName": issuer_name,
        "eventName": _localized(event_name),
        "reviewStatus": "UNDER_REVIEW",
        "hexBackgroundColor": get_theme_hex_background(),
        "dateTime": {
            "start": format_iso_date(start, tz=tz),
            "end": format_iso_date(end, tz=tz),
        },
    }
    # Google's EventVenue requires both name and address; fall back to whichever exists.
    if venue_name or address:
        cls["venue"] = {
            "name": _localized(venue_name or t.cast(str, address)),
            "address": _localized(address or t.cast(str, venue_name)),
        }
    if logo := _image(logo_url):
        cls["logo"] = logo
    if hero := _image(hero_url):
        cls["heroImage"] = hero
    return cls


def build_ticket_payload(ticket: Ticket) -> dict[str, t.Any]:
    """Build the fat-JWT payload for a ticket.

    Field resolution (venue, sector, seat, price) mirrors
    ``ApplePassGenerator._build_pass_data`` so both rails show the same data.

    Args:
        ticket: The ticket to build a payload for.

    Returns:
        ``{"eventTicketClasses": [...], "eventTicketObjects": [...]}``
    """
    event = ticket.event
    org = event.organization
    tz = get_event_timezone(event)

    venue = None
    if ticket.tier.venue:
        venue = ticket.tier.venue
    elif ticket.venue:
        venue = ticket.venue
    elif event.venue:
        venue = event.venue
    venue_name = venue.name if venue else None
    address = (venue.full_address() if venue else None) or event.address or None

    sector = None
    if ticket.tier.sector:
        sector = ticket.tier.sector
    elif ticket.sector:
        sector = ticket.sector
    seat_label = ticket.seat.label if ticket.seat else None

    price, currency = resolve_ticket_price(ticket)

    cls = _build_class(
        class_id=_pass_id("event", event.id),
        issuer_name=org.name,
        event_name=event.name,
        start=event.start,
        end=event.end,
        tz=tz,
        venue_name=venue_name,
        address=address,
        logo_url=_org_logo_url(org),
        hero_url=_event_cover_url(event),
    )

    obj: dict[str, t.Any] = {
        "id": _pass_id("ticket", ticket.id),
        "classId": cls["id"],
        "state": "ACTIVE",
        "barcode": {"type": "QR_CODE", "value": str(ticket.id)},
        "ticketType": _localized(ticket.tier.name),
        "validTimeInterval": {"end": {"date": format_iso_date(event.end + PASS_EXPIRATION_GRACE_PERIOD, tz=tz)}},
        "textModulesData": [{"id": "price", "header": "Price", "body": format_price(price, currency)}],
    }
    if ticket.guest_name:
        obj["ticketHolderName"] = ticket.guest_name
    seat_info: dict[str, t.Any] = {}
    if sector:
        seat_info["section"] = _localized(sector.name)
    if seat_label:
        seat_info["seat"] = _localized(seat_label)
    if seat_info:
        obj["seatInfo"] = seat_info

    return {"eventTicketClasses": [cls], "eventTicketObjects": [obj]}


def build_series_pass_payload(held_pass: HeldSeriesPass) -> dict[str, t.Any]:
    """Build the fat-JWT payload for a held series pass.

    Event-shaped fields are derived from the covered events as a whole,
    mirroring ``ApplePassGenerator._build_series_pass_data``: the soonest
    upcoming covered event is the representative (falling back to the most
    recent past one), and the pass stays valid until the latest covered end.

    Args:
        held_pass: The held series pass to build a payload for.

    Returns:
        ``{"eventTicketClasses": [...], "eventTicketObjects": [...]}``
    """
    series_pass = held_pass.series_pass
    event_series = series_pass.event_series
    org = event_series.organization

    events = [link.event for link in series_pass.tier_links.select_related("event").all()]
    now = timezone.now()
    upcoming = sorted((event for event in events if event.end >= now), key=lambda event: event.start)
    representative = upcoming[0] if upcoming else (max(events, key=lambda event: event.start, default=None))

    if representative is not None:
        venue = representative.venue
        venue_name = venue.name if venue else None
        address = (venue.full_address() if venue else None) or representative.address or None
        tz = get_event_timezone(representative)
        event_start = representative.start
        hero_url = _event_cover_url(representative)
    else:
        venue_name = None
        address = None
        tz = get_organization_timezone(org)
        event_start = held_pass.created_at
        hero_url = None

    event_end = max((event.end for event in events), default=event_start)

    cls = _build_class(
        class_id=_pass_id("series", series_pass.id),
        issuer_name=org.name,
        event_name=series_pass.name,
        start=event_start,
        end=event_end,
        tz=tz,
        venue_name=venue_name,
        address=address,
        logo_url=_org_logo_url(org),
        hero_url=hero_url,
    )

    obj: dict[str, t.Any] = {
        "id": _pass_id("pass", held_pass.id),
        "classId": cls["id"],
        "state": "ACTIVE",
        "barcode": {"type": "QR_CODE", "value": held_pass.qr_payload},
        "ticketType": _localized("Series Pass"),
        "validTimeInterval": {"end": {"date": format_iso_date(event_end + PASS_EXPIRATION_GRACE_PERIOD, tz=tz)}},
        "textModulesData": [
            {"id": "price", "header": "Price", "body": format_price(held_pass.price_paid, series_pass.currency)}
        ],
    }
    return {"eventTicketClasses": [cls], "eventTicketObjects": [obj]}


def build_membership_payload(member: OrganizationMember) -> dict[str, t.Any]:
    """Build the fat-JWT payload for a membership card (generic pass).

    The tier is baked into the OBJECT ID (``membercard.<member_id>-<tier_id>``):
    Google treats a save JWT whose object ID already exists as a no-op (it serves
    the stored object without updating it), so a tier change must mint a NEW
    object for the re-download to show the new tier. The superseded card lingers
    in the user's wallet until manually removed — hence the note text module.
    The barcode, by contrast, is always the stable ``member:<member_id>`` payload.

    Args:
        member: The organization member to build a card payload for.

    Returns:
        ``{"genericClasses": [...], "genericObjects": [...]}``
    """
    org = member.organization
    tz = get_organization_timezone(org)

    cls: dict[str, t.Any] = {"id": _pass_id("memberorg", org.id)}

    tier_suffix = str(member.tier_id) if member.tier_id else "base"
    obj: dict[str, t.Any] = {
        "id": _pass_id("membercard", f"{member.id}-{tier_suffix}"),
        "classId": cls["id"],
        "state": "ACTIVE",
        "cardTitle": _localized(org.name),
        "header": _localized(member.user.get_display_name()),
        "hexBackgroundColor": get_theme_hex_background(),
        "barcode": {"type": "QR_CODE", "value": member.qr_payload},
        "textModulesData": [
            {
                "id": "member_since",
                "header": "Member since",
                "body": format_iso_date(member.created_at, tz=tz)[:10],
            },
            {
                "id": "note",
                "header": "Note",
                "body": "Your newest membership card supersedes any older ones.",
            },
        ],
    }
    if member.tier:
        obj["subheader"] = _localized(member.tier.name)
    if logo := _image(_org_logo_url(org)):
        obj["logo"] = logo

    return {"genericClasses": [cls], "genericObjects": [obj]}
