"""The email logo is hosted on the frontend and referenced by absolute URL.

The PNG asset itself lives in the frontend repo (served at
``{frontend_base_url}/revel-email-logo.png``); the backend never reads it from
disk. So the contract the backend owns is that its email chrome references that
exact URL, built from ``frontend_base_url`` — which is what we assert here,
without any dependency on the sibling frontend repo being checked out.
"""

import typing as t

import pytest
from django.template.loader import render_to_string

pytestmark = pytest.mark.django_db

LOGO_FILENAME = "revel-email-logo.png"


def test_base_email_references_hosted_logo_url() -> None:
    """The branded base builds the logo src as ``{frontend_base_url}/revel-email-logo.png``."""
    ctx: dict[str, t.Any] = {"frontend_base_url": "https://letsrevel.example", "context": {}}
    html = render_to_string("emails/_test_probe.html", ctx)
    assert f"https://letsrevel.example/{LOGO_FILENAME}" in html
