"""HTML/markdown sanitization helpers for Revel.

These functions provide the sanitization layer used by ``MarkdownField`` (see
``common/fields.py``) and by backend-rendered content (emails, Telegram). They are
kept separate from the field classes so the security-sensitive allowlists live in
one focused module.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import nh3

# Allowed HTML tags for markdown content - intentionally restrictive
ALLOWED_TAGS = {
    # Text formatting
    "p",
    "br",
    "hr",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "s",
    "code",
    "pre",
    # Headers
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    # Lists
    "ul",
    "ol",
    "li",
    # Links and quotes
    "a",
    "blockquote",
    # Tables
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
}

# Allowed URL schemes - no javascript, data, etc.
ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}

# Allowed attributes per tag
# Note: "rel" on <a> is handled automatically by nh3's link_rel parameter
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "acronym": {"title"},
    "code": {"class"},  # For syntax highlighting
    "th": {"align", "valign", "scope"},
    "td": {"align", "valign"},
}


def _filter_attributes(
    element: str,
    attribute: str,
    value: str,
) -> str | None:
    """Filter attributes for HTML elements.

    This callback supplements nh3's url_schemes validation by also checking
    URL-decoded values. While nh3 validates schemes directly, it doesn't
    decode URLs first, so encoded attacks like "javascript%3Aalert(1)"
    would bypass the url_schemes check. This filter catches those.

    Args:
        element: The HTML element name
        attribute: The attribute name
        value: The attribute value

    Returns:
        The value if allowed, None to remove
    """
    if element == "a" and attribute == "href":
        # Decode URL to catch encoded attacks like javascript%3A -> javascript:
        decoded_value = unquote(value)
        parsed = urlparse(decoded_value)
        if parsed.scheme and parsed.scheme not in ALLOWED_URL_SCHEMES:
            return None

    return value


def sanitize_html(html: str | None) -> str:
    """Sanitize HTML using nh3 with a safe allowlist.

    This function removes potentially dangerous HTML elements and attributes
    while preserving safe formatting. Images, iframes, SVGs, and other
    potentially dangerous elements are not allowed.

    Args:
        html: The HTML string to sanitize (can be None)

    Returns:
        Sanitized HTML string safe for rendering, or empty string if None
    """
    if not html:
        return ""

    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_filter_attributes,
        url_schemes=ALLOWED_URL_SCHEMES,
        strip_comments=True,
    )


def sanitize_markdown(text: str | None) -> str:
    """Sanitize markdown text by removing potentially dangerous HTML.

    This sanitizes any HTML that might be embedded in markdown content.
    The frontend is responsible for rendering the markdown to HTML.

    Args:
        text: The markdown text to sanitize (can be None)

    Returns:
        Sanitized markdown string, or empty string if text is None
    """
    if not text:
        return ""

    # Sanitize any embedded HTML in the markdown
    return sanitize_html(text)


def render_markdown(text: str | None) -> str:
    """Render markdown to HTML for internal use (emails, Telegram).

    This function is for backend-rendered content only (emails, notifications).
    API responses should return raw markdown for frontend rendering.

    The output is sanitized to prevent XSS attacks.

    Args:
        text: The markdown text to render (can be None)

    Returns:
        Sanitized HTML string, or empty string if text is None
    """
    if not text:
        return ""

    import markdown

    # Render markdown to HTML
    html = markdown.markdown(
        text,
        extensions=["tables", "fenced_code"],
        output_format="html",
    )

    # Sanitize the output to ensure safety
    return sanitize_html(html)
