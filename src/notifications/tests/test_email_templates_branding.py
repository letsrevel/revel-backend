import typing as t

import pytest
from django.template.exceptions import TemplateDoesNotExist
from django.template.loader import render_to_string

from common.tests.branding import LEGACY_BRAND_HEXES
from notifications.enums import NotificationType
from notifications.utils import get_formatted_context_for_template

pytestmark = pytest.mark.django_db

HEARTY_PURPLE = "#8C3CDD"

# Raw context that exercises every Python-generated HTML fragment in notifications.utils:
# the org signature (with logo) and the event link/button.
RAW_CONTEXT: dict[str, t.Any] = {
    "events_count": 1,
    "new_event_count": 1,
    "organization_name": "Org",
    "organization_slug": "org",
    "organization_logo_url": "https://letsrevel.io/logo.png",
    "event_name": "E",
    "event_id": "00000000-0000-0000-0000-000000000000",
}


@pytest.mark.parametrize("ntype", list(NotificationType))
def test_email_template_extends_base_and_has_no_legacy_hex(ntype: NotificationType) -> None:
    tpl = f"notifications/email/{ntype.value}.html"
    # Provide events_count/new_event_count as numbers so blocktranslate count works in
    # series_events_generated / series_pass_extended. All other context keys default to
    # falsy values which templates handle via {% if %} guards.
    # The context is enriched exactly like the real render path (service/templates/base.py)
    # so the Python-generated fragments injected via |safe are scanned too.
    ctx: dict[str, t.Any] = {
        "frontend_base_url": "https://letsrevel.io",
        "user": None,
        "context": get_formatted_context_for_template(RAW_CONTEXT),
    }
    try:
        html = render_to_string(tpl, ctx)
    except TemplateDoesNotExist:
        pytest.skip(f"no html template for {ntype}")
    assert "revel-email-logo.png" in html, f"{tpl} not using branded base"
    for legacy in LEGACY_BRAND_HEXES:
        assert legacy not in html, f"{tpl} still contains legacy {legacy}"


def test_python_generated_email_fragments_have_no_legacy_hex() -> None:
    """Guard the HTML built in notifications.utils, which no template literal covers."""
    enriched = get_formatted_context_for_template(RAW_CONTEXT)
    for key in ("org_signature_html", "event_button_html", "event_link_html"):
        for legacy in LEGACY_BRAND_HEXES:
            assert legacy not in enriched[key], f"{key} still contains legacy {legacy}"
        assert HEARTY_PURPLE in enriched[key], f"{key} does not use the brand token"
