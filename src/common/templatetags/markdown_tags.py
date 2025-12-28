"""Template tags for markdown rendering in emails and notifications."""

from django import template
from django.utils.safestring import mark_safe

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

    return mark_safe(render_markdown(value))
