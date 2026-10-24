"""Utility functions for notification formatting."""

import html
import re
import typing as t
from datetime import datetime

import structlog
from django.utils import timezone, translation

from events.utils.frontend_urls import frontend_base_url_for, frontend_base_url_for_domain

logger = structlog.get_logger(__name__)

ChannelType = t.Literal["email", "markdown", "telegram"]

# Brand primary (docs/brand-style-guide.md); mirrors the `a { color: … }` rule in
# common/templates/emails/base.html so inline styles don't override the brand token.
_HEARTY_PURPLE = "#8C3CDD"

_MARKDOWN_SPECIAL_CHARS = re.compile(r"([\[\]()\\`*_{}##+\-!|~>])")


def _escape_markdown(text: str) -> str:
    """Escape markdown special characters in text."""
    return _MARKDOWN_SPECIAL_CHARS.sub(r"\\\1", text)


TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


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


def _resolve_base_url(base_url: str | None) -> str:
    """Return ``base_url`` or, when None, the platform frontend origin."""
    from common.models import SiteSettings

    return base_url if base_url is not None else SiteSettings.get_solo().frontend_base_url


def format_org_signature(
    org_name: str,
    org_slug: str,
    channel: ChannelType = "markdown",
    include_logo: bool = False,
    logo_url: str | None = None,
    base_url: str | None = None,
) -> str:
    """Format organization signature with optional logo and link.

    Args:
        org_name: Organization name
        org_slug: Organization slug for URL
        channel: Output channel type (email, markdown, telegram)
        include_logo: Whether to include logo (email only)
        logo_url: URL to organization logo (required if include_logo=True)
        base_url: Frontend origin for the link (the org's tenant origin when it has an
            active custom domain); defaults to the platform ``frontend_base_url``

    Returns:
        Formatted organization signature

    Examples:
        email: <div><a href="...">Org Name</a></div>
        markdown: [Org Name](https://...)
        telegram: Same as markdown (will be converted to HTML)
    """
    org_url = f"{_resolve_base_url(base_url)}/org/{org_slug}"

    if channel == "email":
        # HTML format for email
        safe_name = html.escape(org_name)
        logo_html = ""
        if include_logo and logo_url:
            logo_style = "height: 32px; margin-right: 8px; vertical-align: middle;"
            logo_html = f'<img src="{html.escape(logo_url)}" alt="{safe_name}" style="{logo_style}">'

        link_style = f"color: {_HEARTY_PURPLE}; text-decoration: none;"
        escaped_url = html.escape(org_url)
        return f'<p style="margin: 0;">{logo_html}<a href="{escaped_url}" style="{link_style}">{safe_name}</a></p>'

    # Markdown format for in-app and telegram
    safe_name = _escape_markdown(org_name)
    return f"[{safe_name}]({org_url})"


def format_event_link(
    event_name: str,
    event_id: str,
    channel: ChannelType = "markdown",
    button: bool = False,
    base_url: str | None = None,
) -> str:
    """Format event link for notifications.

    Args:
        event_name: Event name for link text
        event_id: Event ID for URL
        channel: Output channel type
        button: Whether to format as a button (email only)
        base_url: Frontend origin for the link (the org's tenant origin when it has an
            active custom domain); defaults to the platform ``frontend_base_url``

    Returns:
        Formatted event link

    Examples:
        email button: <a href="..." class="button">View Event</a>
        email link: <a href="...">Event Name</a>
        markdown: [Event Name](https://...)
    """
    event_url = f"{_resolve_base_url(base_url)}/events/{event_id}"

    if channel == "email":
        if button:
            # Solid-purple fallback for clients that strip <style>; the .button class in
            # emails/base.html upgrades this to the vertical brand gradient where supported.
            button_style = (
                f"display: inline-block; padding: 12px 24px; background: {_HEARTY_PURPLE}; "
                "color: white; text-decoration: none; border-radius: 4px; margin: 10px 0;"
            )
            return f'<a href="{event_url}" class="button" style="{button_style}">View Event Details</a>'
        link_style = f"color: {_HEARTY_PURPLE}; text-decoration: none;"
        safe_name = html.escape(event_name)
        return f'<a href="{event_url}" style="{link_style}">{safe_name}</a>'

    # Markdown format
    safe_name = _escape_markdown(event_name)
    return f"[{safe_name}]({event_url})"


def sanitize_for_telegram(content_html: str) -> str:
    """Sanitize HTML for Telegram's HTML parser.

    Telegram only supports a limited subset of HTML tags:
    <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>, <code>, <pre>, <a>

    This function converts or removes unsupported tags.

    Args:
        content_html: HTML or text string to sanitize for Telegram

    Returns:
        Telegram-compatible HTML string
    """
    # Remove unsupported tags but keep their content
    # Headers -> bold
    result = re.sub(r"<h[1-6]>(.*?)</h[1-6]>", r"<b>\1</b>", content_html, flags=re.DOTALL)

    # Paragraphs -> newlines
    result = re.sub(r"<p>(.*?)</p>", r"\1\n", result, flags=re.DOTALL)

    # Line breaks
    result = result.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")

    # Lists -> text with bullets/numbers
    # Unordered lists
    result = re.sub(r"<ul>(.*?)</ul>", r"\1", result, flags=re.DOTALL)
    result = re.sub(r"<li>(.*?)</li>", r"• \1\n", result, flags=re.DOTALL)

    # Ordered lists (simple conversion - won't be perfect)
    result = re.sub(r"<ol>(.*?)</ol>", r"\1", result, flags=re.DOTALL)

    # Blockquotes -> just keep content
    result = re.sub(r"<blockquote>(.*?)</blockquote>", r"\1", result, flags=re.DOTALL)

    # Horizontal rule
    result = result.replace("<hr>", "\n---\n").replace("<hr/>", "\n---\n").replace("<hr />", "\n---\n")

    # Remove any remaining unsupported tags (tables, divs, etc.)
    # Keep only supported tags: b, strong, i, em, u, ins, s, strike, del, code, pre, a
    result = re.sub(r"<(?!/?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a)\b)[^>]+>", "", result)

    # Clean up excessive newlines
    result = re.sub(r"\n{3,}", "\n\n", result)

    # Strip leading/trailing whitespace
    result = result.strip()

    return result


def _find_safe_cut_point(text: str, target: int) -> int:
    """Find the last position in *text* (up to *target*) that is outside an HTML tag or entity.

    Args:
        text: The HTML string to scan.
        target: Maximum character index to consider.

    Returns:
        A safe cut index (never lands inside ``<…>`` or ``&…;``).
    """
    in_tag = False
    in_entity = False
    safe_cut = 0

    for i, ch in enumerate(text[:target]):
        if ch == "<":
            in_tag = True
        elif ch == ">" and in_tag:
            in_tag = False
        elif ch == "&" and not in_tag:
            in_entity = True
        elif ch == ";" and in_entity:
            in_entity = False

        if not in_tag and not in_entity:
            safe_cut = i + 1

    return safe_cut or target


_TELEGRAM_SUPPORTED_TAGS = frozenset({"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a"})


def _close_unclosed_tags(html_fragment: str) -> str:
    """Return closing markup for any Telegram-supported tags opened but not closed.

    Only tags that Telegram's HTML parser recognises are emitted; injecting
    unsupported closing tags (e.g. ``</li>``, ``</br>``) would cause Telegram
    to reject or mis-parse the entire message.

    Tags are closed in reverse (innermost-first) order.

    Args:
        html_fragment: A possibly-truncated HTML string.

    Returns:
        A string of closing tags, e.g. ``"</i></b>"``.
    """
    tag_iter = re.finditer(r"<(/?)(\w+)(?:\s[^>]*)?>", html_fragment)
    stack: list[str] = []

    for match in tag_iter:
        is_close = match.group(1) == "/"
        tag = match.group(2).lower()
        if tag not in _TELEGRAM_SUPPORTED_TAGS:
            continue
        if not is_close:
            stack.append(tag)
            continue

        for i in range(len(stack) - 1, -1, -1):
            if stack[i] == tag:
                stack.pop(i)
                break

    return "".join(f"</{tag}>" for tag in reversed(stack))


def truncate_telegram_html(message: str, max_length: int, suffix: str) -> str:
    """Truncate an HTML message to fit within a character limit.

    Safely truncates without breaking HTML tags or entities, then closes
    any unclosed tags so Telegram's parser accepts the result.

    Args:
        message: The HTML message to truncate.
        max_length: Maximum allowed length (e.g. 4096 or 1024).
        suffix: Text appended after truncation (e.g. a "Read more" link).

    Returns:
        The original message if it fits, otherwise a truncated version
        with the suffix appended and all HTML tags properly closed.
    """
    if len(message) <= max_length:
        return message

    target = max_length - len(suffix)
    if target <= 0:
        return suffix[:max_length]

    cut = _find_safe_cut_point(message, target)
    truncated = message[:cut]
    closing_markup = _close_unclosed_tags(truncated)
    result = truncated + closing_markup + suffix

    # If closing tags pushed us over the limit, shorten and retry once.
    if len(result) > max_length:
        overshoot = len(result) - max_length
        cut = _find_safe_cut_point(message, max(0, cut - overshoot))
        truncated = message[:cut]
        closing_markup = _close_unclosed_tags(truncated)
        result = truncated + closing_markup + suffix

    return result


# ponytail: email links may be up to ORG_LINK_TTL seconds stale after a custom-domain status
# change or a kill-switch flip. Upgrade path: invalidate ``org_link:v1:<org_id>`` from
# ``custom_domain_service`` on domain status / ``custom_domain_enabled`` changes.
ORG_LINK_TTL = 60
_ORG_LINK_CACHE_KEY = "org_link:v1:{org_id}"


class _OrgLinkInfo(t.TypedDict):
    """What enrichment needs from an organization: its id, logo presence and custom-domain state."""

    id: str
    has_logo: bool
    custom_domain: str | None
    custom_domain_enabled: bool


def _context_org_link_info(context: dict[str, t.Any]) -> _OrgLinkInfo | None:
    """Return the organization facts a notification context needs (cached, fail-open).

    Identifies the org by ``organization_id`` when present, else by ``organization_slug``.
    An id lookup is cached per organization for ``ORG_LINK_TTL`` seconds so a bulk send
    (several rendered bodies per recipient) costs one query instead of one per body; a
    slug-only context always reads the DB. Cache failures degrade to the DB lookup.

    Args:
        context: Raw notification context dict.

    Returns:
        The organization info, or None when the context names none or it no longer exists.
    """
    from django.core.cache import cache

    from events.models import Organization

    key: str | None = None
    if organization_id := context.get("organization_id"):
        lookup = {"id": organization_id}
        key = _ORG_LINK_CACHE_KEY.format(org_id=organization_id)
        try:
            cached: _OrgLinkInfo | None = cache.get(key)
        except Exception:
            logger.warning("org_link_cache_get_failed", exc_info=True)
            cached = None
        if cached is not None:
            return cached
    elif organization_slug := context.get("organization_slug"):
        lookup = {"slug": organization_slug}
    else:
        return None
    org = (
        Organization.objects.only("id", "logo", "logo_thumbnail", "custom_domain", "custom_domain_enabled")
        .filter(**lookup)
        .first()
    )
    if org is None:
        return None
    info = _OrgLinkInfo(
        id=str(org.id),
        has_logo=bool(org.logo_thumbnail or org.logo),
        custom_domain=org.custom_domain,
        custom_domain_enabled=org.custom_domain_enabled,
    )
    if key is not None:
        try:
            cache.set(key, info, ORG_LINK_TTL)
        except Exception:
            logger.warning("org_link_cache_set_failed", exc_info=True)
    return info


def _org_logo_absolute_url(info: _OrgLinkInfo | None) -> str | None:
    """Return the absolute URL of an organization's logo, or None when it has no logo.

    Points at the stable ``/api/organizations/{id}/logo`` endpoint (the same indirection
    the Google Wallet links use): raw media URLs die when a logo is replaced, and emails
    live on. That endpoint serves the thumbnail, falling back to the logo.

    Args:
        info: The organization facts from :func:`_context_org_link_info`, or None.

    Returns:
        Absolute logo URL, or None when the org is missing or has no logo.
    """
    from django.conf import settings
    from django.urls import reverse

    if info is None or not info["has_logo"]:
        return None
    path = reverse("api:organization_logo", kwargs={"organization_id": info["id"]})
    return f"{settings.BASE_URL.rstrip('/')}{path}"


def _base_url_for_info(info: _OrgLinkInfo | None) -> str:
    """Return the frontend origin for the organization facts (platform when None)."""
    if info is None:
        return frontend_base_url_for(None)
    return frontend_base_url_for_domain(info["custom_domain"], info["custom_domain_enabled"])


def _add_org_fields(enriched: dict[str, t.Any], org: t.Callable[[], _OrgLinkInfo | None]) -> None:
    """Add the organization signature, logo URL and ``organization_url`` to ``enriched``.

    Args:
        enriched: The context being enriched (mutated in place).
        org: Lazy accessor for the context's organization facts (memoized by the caller).
    """
    # Unless the caller supplied a logo URL (an explicit empty value opts out).
    if "organization_logo_url" not in enriched:
        logo_url = _org_logo_absolute_url(org())
        if logo_url:
            enriched["organization_logo_url"] = logo_url
    org_logo_url = enriched.get("organization_logo_url")
    base_url = _base_url_for_info(org())

    # HTML version with optional logo (for email)
    enriched["org_signature_html"] = format_org_signature(
        enriched["organization_name"],
        enriched["organization_slug"],
        channel="email",
        include_logo=bool(org_logo_url),
        logo_url=org_logo_url,
        base_url=base_url,
    )

    # Markdown version (for in-app and telegram)
    enriched["org_signature_md"] = format_org_signature(
        enriched["organization_name"],
        enriched["organization_slug"],
        channel="markdown",
        base_url=base_url,
    )

    # Also create direct org URL
    enriched["organization_url"] = f"{base_url}/org/{enriched['organization_slug']}"


def _add_event_fields(enriched: dict[str, t.Any], base_url: str) -> None:
    """Add ``event_url`` and the email/markdown event links to ``enriched``.

    Args:
        enriched: The context being enriched (mutated in place).
        base_url: Frontend origin for the links (tenant origin or platform).
    """
    event_name, event_id = enriched["event_name"], enriched["event_id"]
    enriched["event_url"] = f"{base_url}/events/{event_id}"
    enriched["event_button_html"] = format_event_link(
        event_name, event_id, channel="email", button=True, base_url=base_url
    )
    enriched["event_link_html"] = format_event_link(
        event_name, event_id, channel="email", button=False, base_url=base_url
    )
    enriched["event_link_md"] = format_event_link(event_name, event_id, channel="markdown", base_url=base_url)


def get_formatted_context_for_template(
    context: dict[str, t.Any],
    user_language: str = "en",
) -> dict[str, t.Any]:
    """Prepare context for template rendering with formatted dates and links.

    This function takes the raw notification context and enriches it with:
    - Formatted datetime strings
    - Organization signature (HTML and markdown), plus ``organization_logo_url``
      when the organization has a logo
    - Event links

    Links follow the organization's frontend origin (its active custom domain when it
    has one, else the platform). The organization is identified by ``organization_id`` or
    ``organization_slug`` in the context and looked up lazily (cached 60s by id, see
    :func:`_context_org_link_info`), serving both the logo and the origin.

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
                # Only add formatted versions if neither formatted nor short versions exist.
                # Pre-formatted values use event timezone; reformatting from the raw ISO string
                # (which may be in UTC) could lose the correct event timezone and lead to
                # inconsistencies between *_formatted and *_short.
                formatted_key = f"{field}_formatted"
                short_key = f"{field}_short"
                if formatted_key not in enriched and short_key not in enriched:
                    enriched[formatted_key] = format_datetime(enriched[field], format_type="full")
                    enriched[short_key] = format_datetime(enriched[field], format_type="short")

        # One lazy org lookup per call (itself cached across calls by org id): it serves
        # the logo and the tenant origin for every link.
        org_cache: list[_OrgLinkInfo | None] = []

        def org() -> _OrgLinkInfo | None:
            if not org_cache:
                org_cache.append(_context_org_link_info(enriched))
            return org_cache[0]

        if "organization_name" in enriched and "organization_slug" in enriched:
            _add_org_fields(enriched, org)
        if "event_name" in enriched and "event_id" in enriched:
            _add_event_fields(enriched, _base_url_for_info(org()))

    return enriched
