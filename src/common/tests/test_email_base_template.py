import typing as t

import pytest
from django.template.loader import render_to_string

from common.tests.branding import LEGACY_BRAND_HEXES

pytestmark = pytest.mark.django_db

CTX: dict[str, t.Any] = {"frontend_base_url": "https://letsrevel.io", "context": {}}


def test_base_has_brand_gradient_and_logo() -> None:
    html = render_to_string("emails/_test_probe.html", CTX)
    assert "#8C3CDD" in html and "#E6332A" in html
    assert "revel-email-logo.png" in html
    assert "https://letsrevel.io" in html


def test_base_has_no_legacy_colors() -> None:
    html = render_to_string("emails/_test_probe.html", CTX)
    for legacy in LEGACY_BRAND_HEXES:
        assert legacy not in html, f"legacy color {legacy} leaked into base"
