"""Plain-text email parts must not HTML-escape names (#1340); HTML parts must keep escaping."""

import typing as t
from pathlib import Path

import pytest
from django.template.loader import render_to_string

from accounts.models import RevelUser
from common.utils import render_plain_text
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.dispatcher import create_notification
from notifications.service.templates.registry import get_template

pytestmark = pytest.mark.django_db

ORG = "Rock & Roll"
EVENT = "O'Brien's <b>Night</b>"


@pytest.fixture
def event_open_notification(regular_user: RevelUser) -> Notification:
    """An EVENT_OPEN notification whose names contain HTML-special characters."""
    return create_notification(
        notification_type=NotificationType.EVENT_OPEN,
        user=regular_user,
        context={
            "event_id": "00000000-0000-0000-0000-000000000001",
            "event_name": EVENT,
            "event_start": "2025-12-01T18:00:00Z",
            "event_start_formatted": "Saturday, December 01, 2025 at 6:00 PM UTC",
            "event_location": "Test Venue",
            "event_url": "https://example.com/events/test",
            "organization_id": "00000000-0000-0000-0000-000000000002",
            "organization_name": ORG,
            "event_end": None,
            "event_description": "",
            "rsvp_required": False,
            "questionnaire_required": False,
            "tickets_available": False,
        },
    )


def test_text_part_renders_names_literally(event_open_notification: Notification) -> None:
    """Plain-text body shows `&` and `'` as typed, with no entities."""
    body = get_template(NotificationType.EVENT_OPEN).get_email_text_body(event_open_notification)

    assert ORG in body
    assert EVENT in body
    assert "&amp;" not in body
    assert "&#x27;" not in body


def test_html_part_still_escapes(event_open_notification: Notification) -> None:
    """HTML body keeps autoescaping (XSS guard)."""
    html = t.cast(str, get_template(NotificationType.EVENT_OPEN).get_email_html_body(event_open_notification))

    assert "Rock &amp; Roll" in html
    assert "<b>Night</b>" not in html
    assert "&lt;b&gt;Night&lt;/b&gt;" in html


def test_render_plain_text_helper(tmp_path: Path, settings: t.Any) -> None:
    """The helper renders variables literally; render_to_string escapes the same template."""
    (tmp_path / "t.txt").write_text("Hi {{ x }}")
    settings.TEMPLATES = [
        {**settings.TEMPLATES[0], "DIRS": [str(tmp_path), *settings.TEMPLATES[0].get("DIRS", [])]},
    ]

    assert render_plain_text("t.txt", {"x": ORG}) == "Hi Rock & Roll"
    assert render_to_string("t.txt", {"x": ORG}) == "Hi Rock &amp; Roll"
