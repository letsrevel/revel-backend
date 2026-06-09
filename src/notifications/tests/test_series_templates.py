"""Tests for the SERIES_EVENTS_GENERATED notification template.

Regression coverage for #461: the digest sent when recurring events are
materialized had Django body templates but no registered ``NotificationTemplate``
subclass, so email/Telegram delivery raised ``ValueError`` at ``get_template``.
"""

import pytest

from accounts.models import RevelUser
from notifications.context_schemas import NOTIFICATION_CONTEXT_SCHEMAS, SeriesEventsGeneratedContext
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import (
    get_template,
    is_template_registered,
)
from notifications.service.templates.series_templates import SeriesEventsGeneratedTemplate

pytestmark = pytest.mark.django_db


@pytest.fixture
def digest_user(django_user_model: type[RevelUser]) -> RevelUser:
    """A user to receive the series digest."""
    return django_user_model.objects.create_user(
        username="digest@example.com",
        email="digest@example.com",
        password="password",
        first_name="Di",
        last_name="Gest",
    )


def _make_notification(user: RevelUser, *, events_count: int) -> Notification:
    """Build a SERIES_EVENTS_GENERATED notification with a valid context."""
    context: SeriesEventsGeneratedContext = {
        "organization_id": "org-id",
        "organization_name": "Acme Org",
        "event_series_id": "series-id",
        "event_series_name": "Weekly Standup",
        "events_count": events_count,
        "series_url": "https://example.com/events/acme/series/weekly-standup",
    }
    return Notification.objects.create(
        user=user,
        notification_type=NotificationType.SERIES_EVENTS_GENERATED,
        context=context,
    )


class TestSeriesEventsGeneratedTemplate:
    """The template must produce a title/subject and render every channel."""

    def test_template_is_registered(self) -> None:
        """The fix for #461: the type must resolve to the template instance."""
        assert is_template_registered(NotificationType.SERIES_EVENTS_GENERATED)
        assert isinstance(
            get_template(NotificationType.SERIES_EVENTS_GENERATED),
            SeriesEventsGeneratedTemplate,
        )

    def test_in_app_title_singular(self, digest_user: RevelUser) -> None:
        """Singular count uses singular phrasing and names the series."""
        notification = _make_notification(digest_user, events_count=1)
        title = SeriesEventsGeneratedTemplate().get_in_app_title(notification)
        assert "1 new event scheduled" in title
        assert "Weekly Standup" in title

    def test_in_app_title_plural(self, digest_user: RevelUser) -> None:
        """Plural count uses plural phrasing and the number."""
        notification = _make_notification(digest_user, events_count=3)
        title = SeriesEventsGeneratedTemplate().get_in_app_title(notification)
        assert "3 new events scheduled" in title
        assert "Weekly Standup" in title

    def test_email_subject_singular(self, digest_user: RevelUser) -> None:
        """Singular subject names the series."""
        notification = _make_notification(digest_user, events_count=1)
        subject = SeriesEventsGeneratedTemplate().get_email_subject(notification)
        assert "Weekly Standup" in subject

    def test_email_subject_plural(self, digest_user: RevelUser) -> None:
        """Plural subject includes the count."""
        notification = _make_notification(digest_user, events_count=5)
        subject = SeriesEventsGeneratedTemplate().get_email_subject(notification)
        assert "5 new events" in subject
        assert "Weekly Standup" in subject

    def test_all_channels_render(self, digest_user: RevelUser) -> None:
        """Every channel body renders without raising (the delivery path)."""
        notification = _make_notification(digest_user, events_count=2)
        template = SeriesEventsGeneratedTemplate()

        in_app_body = template.get_in_app_body(notification)
        email_text = template.get_email_text_body(notification)
        email_html = template.get_email_html_body(notification)
        telegram_body = template.get_telegram_body(notification)

        for body in (in_app_body, email_text, email_html, telegram_body):
            assert body
            assert "Weekly Standup" in body
        # The series URL must survive into the user-facing bodies.
        assert notification.context["series_url"] in in_app_body
        assert notification.context["series_url"] in email_text


def test_every_context_schema_has_a_registered_template() -> None:
    """Guardrail (#461): a context schema without a template fails delivery.

    Every ``NotificationType`` that ships a context schema is deliverable, so it
    must also have a registered template. This would have caught the missing
    SERIES_EVENTS_GENERATED template at CI time.
    """
    missing = sorted(nt.value for nt in NOTIFICATION_CONTEXT_SCHEMAS if not is_template_registered(nt))
    assert not missing, f"NotificationTypes with a context schema but no registered template: {missing}"
