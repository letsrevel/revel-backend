import json
import typing as t

from django import template

register = template.Library()


@register.filter
def tojson(value: t.Any) -> str:
    """Convert a Python object to a JSON string."""
    return json.dumps(value, indent=4)
