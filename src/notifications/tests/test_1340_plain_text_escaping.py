"""Plain-text email parts must not HTML-escape names (#1340); HTML parts must keep escaping."""

import typing as t

import pytest
from django.template import Context, Template

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


def test_render_plain_text_helper() -> None:
    """The helper disables autoescape; a plain Template stays escaped."""
    assert Template("{{ x }}").render(Context({"x": ORG})) == "Rock &amp; Roll"
    assert render_plain_text("notifications/email/_footer.txt", {"x": ORG}) is not None
