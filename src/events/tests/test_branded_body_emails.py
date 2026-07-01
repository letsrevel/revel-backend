"""Tests: branded HTML bodies for text-only / inline-string emails (Task 6)."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.template.loader import render_to_string

pytestmark = pytest.mark.django_db

HTML_TEMPLATES = [
    "events/emails/guest_rsvp_confirmation_body.html",
    "events/emails/guest_ticket_confirmation_body.html",
    "emails/revenue_report.html",
    "events/emails/attendee_invoice_email.html",
    "events/emails/attendee_credit_note_email.html",
    "events/emails/platform_fee_invoice_email.html",
    "accounts/emails/referral_payout_email.html",
]

LEGACY_HEX = ("#667eea", "#764ba2", "#28a745", "#2196F3")


@pytest.mark.parametrize("tpl", HTML_TEMPLATES)
def test_branded_body_email(tpl: str) -> None:
    ctx: dict[str, t.Any] = {
        "frontend_base_url": "https://letsrevel.io",
        "confirmation_link": "https://letsrevel.io/c",
        "event_name": "E",
        "tier_name": "T",
        "invoice_number": "INV-1",
        "credit_note_number": "CN-1",
        "organization_name": "Org",
        "context": {},
    }
    html = render_to_string(tpl, ctx)
    assert "revel-email-logo.png" in html, f"{tpl}: logo missing"
    for legacy in LEGACY_HEX:
        assert legacy not in html, f"{tpl}: legacy hex {legacy} found"


def test_guest_rsvp_send_email_passes_html_body(settings: t.Any) -> None:
    """send_guest_rsvp_confirmation calls send_email with a non-null html_body."""
    from events.tasks.attendees import send_guest_rsvp_confirmation

    with (
        patch("events.tasks.attendees.SiteSettings") as mock_ss,
        patch("events.tasks.attendees.send_email") as mock_send,
    ):
        site = MagicMock()
        site.frontend_base_url = "https://letsrevel.io"
        mock_ss.get_solo.return_value = site

        send_guest_rsvp_confirmation(email="a@b.com", token="tok", event_name="My Event")

        assert mock_send.called
        kwargs = mock_send.call_args.kwargs
        assert kwargs.get("html_body") is not None, "html_body must be set"
