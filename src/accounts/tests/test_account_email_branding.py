"""Guard tests: every accounts transactional HTML template must extend the branded base."""

import typing as t

import pytest
from django.template.loader import render_to_string

pytestmark = pytest.mark.django_db

TEMPLATES = [
    "email_verification",
    "account_activation",
    "password_reset",
    "email_change_confirmation",
    "email_change_notice",
    "email_change_completed_old",
    "email_change_completed_new",
    "account_delete",
    "email_verification_reminder",
    "email_verification_final_warning",
    "account_deactivated",
    "data_export_ready",
    "data_export_failed",
    "data_export_failed_admin",
]
LEGACY = ("#667eea", "#764ba2", "#28a745", "#2196F3", "#3498db")


@pytest.mark.parametrize("base", TEMPLATES)
def test_account_email_branded(base: str) -> None:
    ctx: dict[str, t.Any] = {
        "frontend_base_url": "https://letsrevel.io",
        "action_link": "https://letsrevel.io/x",
        "masked_new_email": "a***@b.c",
        "old_email": "a@b.c",
        "new_email": "c@d.e",
        "download_link": "https://letsrevel.io/d",
        "error": "boom",
        "username": "u",
        "context": {},
    }
    html = render_to_string(f"accounts/emails/{base}_body.html", ctx)
    assert "revel-email-logo.png" in html, f"{base} not on branded base"
    for legacy in LEGACY:
        assert legacy not in html, f"{base} still has {legacy}"
