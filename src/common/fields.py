"""Custom Django fields for Revel.

This module provides custom field types with built-in sanitization and security features.
"""

from __future__ import annotations

import typing as t
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import bleach
import markdown as md
from django.contrib.gis.db import models

if TYPE_CHECKING:
    pass

# Allowed HTML tags after markdown conversion
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
    # Images
    "img",
    # Embedded content
    "iframe",
}

# Allowed domains for iframe embeds
ALLOWED_IFRAME_DOMAINS = {
    "www.youtube.com",
    "youtube.com",
    "www.youtube-nocookie.com",
    "youtube-nocookie.com",
    "player.vimeo.com",
    "vimeo.com",
}


def _allow_img_attributes(tag: str, name: str, value: str) -> bool:
    """Validate img tag attributes.

    Only allows safe image sources (https URLs or data URIs).

    Args:
        tag: The HTML tag name
        name: The attribute name
        value: The attribute value

    Returns:
        True if the attribute is allowed, False otherwise
    """
    if tag == "img":
        # Allow alt and title for all images
        if name in ("alt", "title", "width", "height"):
            return True

        # Strict validation for src attribute
        if name == "src":
            # Allow data URIs for inline images
            if value.startswith("data:image/"):
                return True
            # Only allow HTTPS URLs for security
            parsed = urlparse(value)
            return parsed.scheme == "https"

    return False


def _allow_iframe_attributes(tag: str, name: str, value: str) -> bool:
    """Validate iframe tag attributes.

    Only allows embeds from approved domains (YouTube, Vimeo).

    Args:
        tag: The HTML tag name
        name: The attribute name
        value: The attribute value

    Returns:
        True if the attribute is allowed, False otherwise
    """
    if tag == "iframe":
        # Allow basic iframe attributes
        if name in ("width", "height", "frameborder", "allowfullscreen", "title"):
            return True

        # Strict validation for src - only allow approved embed domains
        if name == "src":
            parsed = urlparse(value)
            # Must use https
            if parsed.scheme != "https":
                return False
            # Must be from an approved domain
            return parsed.netloc in ALLOWED_IFRAME_DOMAINS

    return False


def _allow_attributes(tag: str, name: str, value: str) -> bool:
    """Main attribute validator that combines all validation rules.

    Args:
        tag: The HTML tag name
        name: The attribute name
        value: The attribute value

    Returns:
        True if the attribute is allowed, False otherwise
    """
    # Standard allowed attributes for all tags
    standard_attrs = {
        "a": ["href", "title", "rel"],
        "abbr": ["title"],
        "acronym": ["title"],
        "code": ["class"],  # For syntax highlighting
        "th": ["align", "valign", "scope"],
        "td": ["align", "valign"],
    }

    # Check standard attributes first
    if tag in standard_attrs and name in standard_attrs[tag]:
        # Additional validation for href to ensure safe protocols
        if tag == "a" and name == "href":
            parsed = urlparse(value)
            return parsed.scheme in ("http", "https", "mailto", "")
        return True

    # Check img-specific rules
    if tag == "img":
        return _allow_img_attributes(tag, name, value)

    # Check iframe-specific rules
    if tag == "iframe":
        return _allow_iframe_attributes(tag, name, value)

    return False


# Allowed protocols for links
ALLOWED_PROTOCOLS = {"http", "https", "mailto"}


def sanitize_html(html: str) -> str:
    """Sanitize HTML using bleach with a safe allowlist.

    This function removes potentially dangerous HTML elements and attributes
    while preserving safe formatting generated from markdown.

    Args:
        html: The HTML string to sanitize

    Returns:
        Sanitized HTML string safe for rendering
    """
    result: str = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=_allow_attributes,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,  # Strip disallowed tags instead of escaping them
        strip_comments=True,  # Remove HTML comments
    )
    return result


def render_markdown(text: str | None) -> str:
    """Convert markdown to sanitized HTML.

    Args:
        text: The markdown text to render (can be None)

    Returns:
        Sanitized HTML string, or empty string if text is None
    """
    if not text:
        return ""

    # Convert markdown to HTML with extensions
    html = md.markdown(
        text,
        extensions=[
            "tables",  # GitHub-style tables
            "fenced_code",  # Code blocks with ```
            "nl2br",  # Convert newlines to <br>
            "codehilite",  # Syntax highlighting for code blocks
        ],
        extension_configs={
            "codehilite": {
                "css_class": "highlight",
                "linenums": False,
            }
        },
        output_format="html",
    )

    # Sanitize the HTML to prevent XSS
    return sanitize_html(html)


if TYPE_CHECKING:

    class MarkdownField(models.TextField[str | None, str | None]):
        """Type stub for MarkdownField."""

        ...

else:

    class MarkdownField(models.TextField):  # type: ignore[misc]
        """A TextField that stores markdown and provides sanitized HTML rendering.

        This field stores raw markdown in the database and provides a property
        to render it as safe, sanitized HTML. It prevents XSS attacks by using
        bleach to sanitize the HTML output.

        Usage:
            class MyModel(models.Model):
                description = MarkdownField(blank=True, null=True)

            # Access raw markdown
            obj.description  # Returns raw markdown string

            # Access rendered HTML (read-only property added to model instance)
            obj.description_html  # Returns sanitized HTML

        Supported Markdown Features:
            - Headers (# ## ###)
            - Bold, italic, strikethrough
            - Lists (ordered and unordered)
            - Links
            - Images (HTTPS only)
            - Code blocks with syntax highlighting
            - Tables
            - Blockquotes
            - Embedded content (YouTube, Vimeo iframes)

        Security:
            - All HTML output is sanitized using bleach
            - Only safe HTML tags and attributes are allowed
            - Image sources must be HTTPS or data URIs
            - Iframe embeds limited to approved domains
            - All JavaScript and unsafe content is stripped
        """

        description = "A field that stores markdown and renders safe HTML"

        def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
            """Initialize the MarkdownField."""
            super().__init__(*args, **kwargs)

        def contribute_to_class(self, cls: type[models.Model], name: str, private_only: bool = False) -> None:
            """Add the field to the model and create a rendered HTML property.

            This method is called by Django when the field is added to a model.
            It creates a property on the model that returns the rendered HTML.

            Args:
                cls: The model class this field is being added to
                name: The name of this field
                private_only: Whether this field is only for private use
            """
            super().contribute_to_class(cls, name, private_only=private_only)

            # Create a property for the rendered HTML
            html_property_name = f"{name}_html"

            def get_html(instance: models.Model) -> str:
                """Get the rendered HTML for this field."""
                value = getattr(instance, name)
                return render_markdown(value)

            # Add the property to the model class
            setattr(cls, html_property_name, property(get_html))
