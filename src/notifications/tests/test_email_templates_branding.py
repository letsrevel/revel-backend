import typing as t

import pytest
from django.template.exceptions import TemplateDoesNotExist
from django.template.loader import render_to_string

from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db

LEGACY = ("#667eea", "#764ba2", "#28a745", "#4CAF50", "#dc3545", "#2196F3", "#ff9800")


@pytest.mark.parametrize("ntype", list(NotificationType))
def test_email_template_extends_base_and_has_no_legacy_hex(ntype: NotificationType) -> None:
    tpl = f"notifications/email/{ntype.value}.html"
    # Provide events_count/new_event_count as numbers so blocktranslate count works in
    # series_events_generated / series_pass_extended. All other context keys default to
    # falsy values which templates handle via {% if %} guards.
    ctx: dict[str, t.Any] = {
        "frontend_base_url": "https://letsrevel.io",
        "user": None,
        "context": {"events_count": 1, "new_event_count": 1},
    }
    try:
        html = render_to_string(tpl, ctx)
    except TemplateDoesNotExist:
        pytest.skip(f"no html template for {ntype}")
    assert "revel-email-logo.png" in html, f"{tpl} not using branded base"
    for legacy in LEGACY:
        assert legacy not in html, f"{tpl} still contains legacy {legacy}"
