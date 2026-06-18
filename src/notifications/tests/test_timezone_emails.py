"""Regression tests for issue #511 — event times always rendered in the event's timezone.

Covers the two context-builders that historically bypassed ``format_event_datetime``
(the INVITATION_RECEIVED signal, which sent raw ISO strings, and the
NEW_EVENT_FROM_FOLLOWED_* signal, which used a tz-naive ``strftime``), plus the
shared "all times are in the event's local timezone" disclaimer partial.
"""

import typing as t
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import Point
from django.template.loader import render_to_string

from accounts.models import RevelUser
from events.models import Event, EventInvitation, Organization
from events.models.follow import OrganizationFollow
from geo.models import City
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def owner(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization owner."""
    return django_user_model.objects.create_user(
        username="owner@example.com",
        email="owner@example.com",
        password="password",
        first_name="Org",
        last_name="Owner",
    )


@pytest.fixture
def invited(django_user_model: type[RevelUser]) -> RevelUser:
    """A user who receives an invitation / follows the org."""
    return django_user_model.objects.create_user(
        username="invited@example.com",
        email="invited@example.com",
        password="password",
        first_name="In",
        last_name="Vited",
    )


@pytest.fixture
def vienna(db: None) -> City:
    """Vienna (Europe/Vienna, CET in winter)."""
    return City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="Austria",
        iso2="AT",
        iso3="AUT",
        city_id=511001,
        location=Point(16.3738, 48.2082, srid=4326),
        timezone="Europe/Vienna",
    )


@pytest.fixture
def org(owner: RevelUser) -> Organization:
    """Public organization."""
    return Organization.objects.create(
        name="TZ Org",
        slug="tz-org",
        owner=owner,
        visibility=Organization.Visibility.PUBLIC,
    )


@pytest.fixture
def vienna_event(org: Organization, vienna: City) -> Event:
    """Event in Vienna starting 18:00 UTC (== 19:00 CET) and ending 21:00 UTC (== 22:00 CET)."""
    start = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))
    return Event.objects.create(
        organization=org,
        name="Vienna Gala",
        slug="vienna-gala",
        city=vienna,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        max_attendees=100,
        status=Event.EventStatus.DRAFT,
        start=start,
        end=start + timedelta(hours=3),
    )


# --- Signal context: INVITATION_RECEIVED ---


class TestInvitationContextTimezone:
    """The INVITATION_RECEIVED signal must emit formatted, event-local times and a working URL."""

    def test_context_has_event_local_formatted_times_and_url(
        self,
        vienna_event: Event,
        invited: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Regression: signal previously sent raw ISO ``event_start`` and a ``frontend_url`` the
        templates never read, so invite emails showed no time and a dead button."""
        with patch("notifications.signals.invitation.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                EventInvitation.objects.create(user=invited, event=vienna_event)

        calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.INVITATION_RECEIVED
        ]
        assert len(calls) == 1
        context = calls[0].kwargs["context"]

        # 18:00 UTC -> 19:00 CET, 21:00 UTC -> 22:00 CET
        assert "7:00 PM" in context["event_start_formatted"]
        assert "CET" in context["event_start_formatted"]
        assert "10:00 PM" in context["event_end_formatted"]
        # The "4:00 PM to 4:00 PM" bug: start and end must not collapse to the same string.
        assert context["event_start_formatted"] != context["event_end_formatted"]
        # Templates read context.event_url (not frontend_url).
        assert context["event_url"].endswith(f"/events/{vienna_event.id}")


# --- Signal context: NEW_EVENT_FROM_FOLLOWED_ORG ---


class TestFollowerContextTimezone:
    """The follower "new event" signal must format the start in the event's tz, not UTC."""

    def test_context_start_is_event_local_not_utc(
        self,
        org: Organization,
        vienna_event: Event,
        invited: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Regression: signal previously used ``start.strftime(...)`` which renders in UTC."""
        OrganizationFollow.objects.create(
            user=invited,
            organization=org,
            is_archived=False,
            notify_new_events=True,
        )

        with patch("events.signals.notification_requested.send") as mock_send:
            with django_capture_on_commit_callbacks(execute=True):
                vienna_event.status = Event.EventStatus.OPEN
                vienna_event.save(update_fields=["status"])

        calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("notification_type") == NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG
        ]
        assert len(calls) == 1
        formatted = calls[0].kwargs["context"]["event_start_formatted"]
        # Event-local (19:00 CET), never the 6:00 PM UTC wall time.
        assert "7:00 PM" in formatted
        assert "CET" in formatted
        assert "6:00 PM" not in formatted


# --- Disclaimer partial ---


class TestTimezoneDisclaimer:
    """The shared disclaimer must appear only when an email renders event times."""

    NOTE = "All times are shown in the event's local timezone."

    def test_footer_shows_note_when_event_time_present(self) -> None:
        rendered = render_to_string(
            "notifications/email/_footer.html",
            {"context": {"event_start_formatted": "Friday, February 6, 2026 at 7:00 PM CET"}},
        )
        assert self.NOTE in rendered

    def test_footer_hides_note_without_event_time(self) -> None:
        rendered = render_to_string("notifications/email/_footer.html", {"context": {}})
        assert self.NOTE not in rendered

    def test_footer_txt_shows_note_when_event_time_present(self) -> None:
        rendered = render_to_string(
            "notifications/email/_footer.txt",
            {"context": {"event_start_formatted": "Friday, February 6, 2026 at 7:00 PM CET"}},
        )
        assert self.NOTE in rendered

    def test_time_bearing_email_includes_note(self) -> None:
        """A full time-bearing email (ticket_created) surfaces the disclaimer via the footer."""
        rendered = render_to_string(
            "notifications/email/ticket_created.html",
            {
                "user": {"display_name": "Test User"},
                "context": {
                    "event_name": "Vienna Gala",
                    "event_start_formatted": "Friday, February 6, 2026 at 7:00 PM CET",
                    "event_url": "https://example.com",
                },
            },
        )
        assert self.NOTE in rendered

    def test_non_time_email_omits_note(self) -> None:
        """A membership email carries no event time, so the disclaimer must not appear."""
        rendered = render_to_string(
            "notifications/email/membership_granted.html",
            {
                "user": {"display_name": "Test User"},
                "context": {"organization_name": "TZ Org"},
            },
        )
        assert self.NOTE not in rendered

    def test_pending_invitation_includes_note(self) -> None:
        """pending_invitation has its own footer; the note is included in its details card."""
        rendered = render_to_string(
            "notifications/email/pending_invitation.html",
            {
                "event_name": "Vienna Gala",
                "event_start_formatted": "Friday, February 6, 2026 at 7:00 PM CET",
                "signup_url": "https://example.com/signup",
            },
        )
        assert self.NOTE in rendered

    def test_invitation_email_renders_distinct_start_and_end(self) -> None:
        """invitation_received shows both start and end when provided (regression for blank times)."""
        rendered = render_to_string(
            "notifications/email/invitation_received.html",
            {
                "user": {"display_name": "Test User"},
                "context": {
                    "event_name": "Vienna Gala",
                    "event_start_formatted": "Friday, February 6, 2026 at 7:00 PM CET",
                    "event_end_formatted": "Friday, February 6, 2026 at 10:00 PM CET",
                    "event_url": "https://example.com/events/1",
                },
            },
        )
        assert "7:00 PM CET" in rendered
        assert "10:00 PM CET" in rendered
        assert "https://example.com/events/1" in rendered
        assert self.NOTE in rendered
