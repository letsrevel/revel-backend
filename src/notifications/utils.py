"""Utility functions for notification formatting."""

import re
import typing as t
from datetime import datetime

from django.utils import timezone, translation

ChannelType = t.Literal["email", "markdown", "telegram"]


def format_datetime(
    dt: datetime | str,
    format_type: t.Literal["full", "short"] = "full",
) -> str:
    """Format a datetime for display in notifications.

    Args:
        dt: Datetime object or ISO format string
        format_type: "full" for verbose format, "short" for concise format

    Returns:
        Formatted datetime string with timezone

    Examples:
        full: "Wednesday, November 14, 2025 at 6:30 PM CET"
        short: "Nov 14, 2025 at 6:30 PM"
    """
    # Parse string to datetime if needed
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)

    # Ensure timezone awareness
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)

    # Use the datetime's timezone for formatting
    if format_type == "full":
        # Format: "Wednesday, November 14, 2025 at 6:30 PM CET"
        return dt.strftime("%A, %B %d, %Y at %I:%M %p %Z")
    # Format: "Nov 14, 2025 at 6:30 PM"
    return dt.strftime("%b %d, %Y at %I:%M %p")


def format_org_signature(
    org_name: str,
    org_slug: str,
    channel: ChannelType = "markdown",
    include_logo: bool = False,
    logo_url: str | None = None,
) -> str:
    """Format organization signature with optional logo and link.

    Args:
        org_name: Organization name
        org_slug: Organization slug for URL
        channel: Output channel type (email, markdown, telegram)
        include_logo: Whether to include logo (email only)
        logo_url: URL to organization logo (required if include_logo=True)

    Returns:
        Formatted organization signature

    Examples:
        email: <div><a href="...">Org Name</a></div>
        markdown: [Org Name](https://...)
        telegram: Same as markdown (will be converted to HTML)
    """
    from common.models import SiteSettings

    org_url = f"{SiteSettings.get_solo().frontend_base_url}/org/{org_slug}"

    if channel == "email":
        # HTML format for email
        logo_html = ""
        if include_logo and logo_url:
            logo_style = "height: 32px; margin-right: 8px; vertical-align: middle;"
            logo_html = f'<img src="{logo_url}" alt="{org_name}" style="{logo_style}">'

        link_style = "color: #2196F3; text-decoration: none;"
        return f'<p style="margin: 0;">{logo_html}<a href="{org_url}" style="{link_style}">{org_name}</a></p>'

    # Markdown format for in-app and telegram
    return f"[{org_name}]({org_url})"


def format_event_link(
    event_name: str,
    event_id: str,
    channel: ChannelType = "markdown",
    button: bool = False,
) -> str:
    """Format event link for notifications.

    Args:
        event_name: Event name for link text
        event_id: Event ID for URL
        channel: Output channel type
        button: Whether to format as a button (email only)

    Returns:
        Formatted event link

    Examples:
        email button: <a href="..." class="button">View Event</a>
        email link: <a href="...">Event Name</a>
        markdown: [Event Name](https://...)
    """
    from common.models import SiteSettings

    event_url = f"{SiteSettings.get_solo().frontend_base_url}/events/{event_id}"

    if channel == "email":
        if button:
            button_style = (
                "display: inline-block; padding: 12px 24px; background: #2196F3; "
                "color: white; text-decoration: none; border-radius: 4px; margin: 10px 0;"
            )
            return f'<a href="{event_url}" class="button" style="{button_style}">View Event Details</a>'
        link_style = "color: #2196F3; text-decoration: none;"
        return f'<a href="{event_url}" style="{link_style}">{event_name}</a>'

    # Markdown format
    return f"[{event_name}]({event_url})"


def sanitize_for_telegram(html: str) -> str:
    """Sanitize HTML for Telegram's HTML parser.

    Telegram only supports a limited subset of HTML tags:
    <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>, <code>, <pre>, <a>

    This function converts or removes unsupported tags.

    Args:
        html: HTML or text string to sanitize for Telegram

    Returns:
        Telegram-compatible HTML string
    """
    # Remove unsupported tags but keep their content
    # Headers -> bold
    html = re.sub(r"<h[1-6]>(.*?)</h[1-6]>", r"<b>\1</b>", html, flags=re.DOTALL)

    # Paragraphs -> newlines
    html = re.sub(r"<p>(.*?)</p>", r"\1\n", html, flags=re.DOTALL)

    # Line breaks
    html = html.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")

    # Lists -> text with bullets/numbers
    # Unordered lists
    html = re.sub(r"<ul>(.*?)</ul>", r"\1", html, flags=re.DOTALL)
    html = re.sub(r"<li>(.*?)</li>", r"• \1\n", html, flags=re.DOTALL)

    # Ordered lists (simple conversion - won't be perfect)
    html = re.sub(r"<ol>(.*?)</ol>", r"\1", html, flags=re.DOTALL)

    # Blockquotes -> just keep content
    html = re.sub(r"<blockquote>(.*?)</blockquote>", r"\1", html, flags=re.DOTALL)

    # Horizontal rule
    html = html.replace("<hr>", "\n---\n").replace("<hr/>", "\n---\n").replace("<hr />", "\n---\n")

    # Remove any remaining unsupported tags (tables, divs, etc.)
    # Keep only supported tags: b, strong, i, em, u, ins, s, strike, del, code, pre, a
    html = re.sub(r"<(?!/?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a)\b)[^>]+>", "", html)

    # Clean up excessive newlines
    html = re.sub(r"\n{3,}", "\n\n", html)

    # Strip leading/trailing whitespace
    html = html.strip()

    return html


def get_formatted_context_for_template(
    context: dict[str, t.Any],
    user_language: str = "en",
) -> dict[str, t.Any]:
    """Prepare context for template rendering with formatted dates and links.

    This function takes the raw notification context and enriches it with:
    - Formatted datetime strings
    - Organization signature (HTML and markdown)
    - Event links

    Args:
        context: Raw notification context dict
        user_language: User's preferred language for date formatting

    Returns:
        Enriched context dict with formatted fields
    """
    # Create a copy to avoid mutating the original
    enriched = context.copy()

    # Activate user's language for formatting
    with translation.override(user_language):
        # Format all datetime fields
        datetime_fields = [
            "event_start",
            "event_end",
            "rsvp_created_at",
            "ticket_created_at",
            "invitation_expires_at",
        ]

        for field in datetime_fields:
            if field in enriched and enriched[field]:
                # Only add formatted versions if they don't already exist
                # (pre-formatted values use event timezone; reformatting would lose that)
                if f"{field}_formatted" not in enriched:
                    enriched[f"{field}_formatted"] = format_datetime(enriched[field], format_type="full")
                if f"{field}_short" not in enriched:
                    enriched[f"{field}_short"] = format_datetime(enriched[field], format_type="short")

        # Add organization signature if org info is present
        if "organization_name" in enriched and "organization_slug" in enriched:
            org_logo_url = enriched.get("organization_logo_url")

            # HTML version with optional logo (for email)
            enriched["org_signature_html"] = format_org_signature(
                enriched["organization_name"],
                enriched["organization_slug"],
                channel="email",
                include_logo=bool(org_logo_url),
                logo_url=org_logo_url,
            )

            # Markdown version (for in-app and telegram)
            enriched["org_signature_md"] = format_org_signature(
                enriched["organization_name"],
                enriched["organization_slug"],
                channel="markdown",
            )

            # Also create direct org URL
            from common.models import SiteSettings

            site_settings = SiteSettings.get_solo()
            enriched["organization_url"] = f"{site_settings.frontend_base_url}/org/{enriched['organization_slug']}"

        # Add event link if event info is present
        if "event_name" in enriched and "event_id" in enriched:
            from common.models import SiteSettings

            site_settings = SiteSettings.get_solo()
            enriched["event_url"] = f"{site_settings.frontend_base_url}/events/{enriched['event_id']}"

            # Button version for email
            enriched["event_button_html"] = format_event_link(
                enriched["event_name"],
                enriched["event_id"],
                channel="email",
                button=True,
            )

            # Link version for email
            enriched["event_link_html"] = format_event_link(
                enriched["event_name"],
                enriched["event_id"],
                channel="email",
                button=False,
            )

            # Markdown version
            enriched["event_link_md"] = format_event_link(
                enriched["event_name"],
                enriched["event_id"],
                channel="markdown",
            )

    return enriched
