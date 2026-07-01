import typing as t

import pytest
from django.template.loader import render_to_string

pytestmark = pytest.mark.django_db

TEMPLATES = [
    "common/emails/malware_detected_user.html",
    "common/emails/malware_detected_superuser.html",
    "events/emails/organization_contact_message_body.html",
    "events/emails/organization_contact_email_verification_body.html",
]
LEGACY = ("#667eea", "#764ba2", "#28a745", "#2196F3", "#3498db")


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_standalone_email_branded(tpl: str) -> None:
    ctx: dict[str, t.Any] = {
        "frontend_base_url": "https://letsrevel.io",
        "filename": "x.pdf", "organization_name": "Org", "message": "hi",
        "sender_name": "A", "sender_email": "a@b.c", "verification_link": "https://letsrevel.io/v",
        "context": {},
    }
    html = render_to_string(tpl, ctx)
    assert "revel-email-logo.png" in html, f"{tpl} not on branded base"
    for legacy in LEGACY:
        assert legacy not in html, f"{tpl} still has {legacy}"
