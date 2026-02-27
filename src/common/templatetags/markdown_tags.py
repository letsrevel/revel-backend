"""Template tags for markdown rendering in emails and notifications."""

import re

from django import template
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe
from markdownify import markdownify

from common.fields import render_markdown

register = template.Library()


@register.filter(is_safe=True)
def markdown(value: str | None) -> str:
    """Render markdown to HTML.

    This filter converts markdown syntax to HTML for use in email templates
    and other backend-rendered content. The output is sanitized and marked safe.

    Usage:
        {% load markdown_tags %}
        {{ event.description|markdown }}

    Args:
        value: Markdown text to render

    Returns:
        Sanitized HTML string
    """
    if not value:
        return ""

    # render_markdown calls nh3.clean() with a strict allowlist, so the output
    # is sanitized and safe to mark as such.
    return mark_safe(render_markdown(value))


@register.filter
def html_to_text(value: str | None) -> str:
    """Convert HTML to plain text, preserving line breaks for block elements.

    Replaces closing block tags and <br> with newlines before stripping all
    remaining HTML tags. Useful for Telegram and plain-text email channels
    where WYSIWYG (Trix) HTML body must be rendered as readable text.

    Usage:
        {% load markdown_tags %}
        {{ context.announcement_body|html_to_text }}
    """
    if not value:
        return ""

    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</(?:div|p|h[1-6]|li|blockquote)>", "\n", text, flags=re.IGNORECASE)
    text = strip_tags(text)
    # Collapse runs of 3+ newlines into 2, and strip trailing whitespace per line
    text = re.sub(r"[ \t]*\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@register.filter
def html_to_markdown(value: str | None) -> str:
    """Convert HTML to Markdown, preserving links and formatting.

    Uses markdownify to convert WYSIWYG (Trix) HTML into Markdown suitable
    for the in-app notification channel where the frontend renders Markdown.

    Usage:
        {% load markdown_tags %}
        {{ context.announcement_body|html_to_markdown }}
    """
    if not value:
        return ""

    return markdownify(value).strip()
