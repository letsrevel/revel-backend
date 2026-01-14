"""Tests for the dashboard calendar endpoint."""

import typing as t
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from freezegun import freeze_time
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Event,
    EventRSVP,
    Organization,
)

pytestmark = pytest.mark.django_db


# Fixtures dashboard_user, dashboard_client, dashboard_setup are in conftest.py


class TestDashboardCalendar:
    """Tests for the dashboard calendar endpoint."""

    def test_calendar_default_returns_current_month(
        self, dashboard_user: RevelUser, organization: Organization
    ) -> None:
        """Test that calling /dashboard/calendar with no params defaults to current month."""
        # Create events in December 2025 and January 2026
        dec_event = Event.objects.create(
            organization=organization,
            name="December Event",
            slug="dec-event",
            status="open",
            start=datetime(2025, 12, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2025, 12, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=dec_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        jan_event = Event.objects.create(
            organization=organization,
            name="January Event",
            slug="jan-event",
            status="open",
            start=datetime(2026, 1, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=jan_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        url = reverse("api:dashboard_calendar")
        with freeze_time("2025-12-01"):
            # Create client inside freeze_time to avoid JWT expiration issues
            refresh = RefreshToken.for_user(dashboard_user)
            client = Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]
            response = client.get(url)

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(dec_event.id) in event_ids
        assert str(jan_event.id) not in event_ids

    def test_calendar_month_view(
        self, dashboard_client: Client, dashboard_user: RevelUser, organization: Organization
    ) -> None:
        """Test month view returns only events in specified month that user is related to."""
        # Create events in different months
        dec_event = Event.objects.create(
            organization=organization,
            name="December Event",
            slug="dec-event",
            status="open",
            start=datetime(2025, 12, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2025, 12, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=dec_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        jan_event = Event.objects.create(
            organization=organization,
            name="January Event",
            slug="jan-event",
            status="open",
            start=datetime(2026, 1, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=jan_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        # Create an event in December but user has no relationship
        Event.objects.create(
            organization=organization,
            name="Unrelated December Event",
            slug="unrelated-dec",
            status="open",
            start=datetime(2025, 12, 20, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2025, 12, 20, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:dashboard_calendar")
        response = dashboard_client.get(url, {"month": "12", "year": "2025"})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        # Only the December event the user RSVP'd to should appear
        assert str(dec_event.id) in event_ids
        assert str(jan_event.id) not in event_ids
        assert len(data) == 1

    def test_calendar_year_view(
        self, dashboard_client: Client, dashboard_user: RevelUser, organization: Organization
    ) -> None:
        """Test year view returns all user-related events in that year."""
        # Create events in different years
        event_2026 = Event.objects.create(
            organization=organization,
            name="2026 Event",
            slug="event-2026",
            status="open",
            start=datetime(2026, 6, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=event_2026, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        event_2027 = Event.objects.create(
            organization=organization,
            name="2027 Event",
            slug="event-2027",
            status="open",
            start=datetime(2027, 1, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2027, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=event_2027, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        url = reverse("api:dashboard_calendar")
        response = dashboard_client.get(url, {"year": "2026"})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(event_2026.id) in event_ids
        assert str(event_2027.id) not in event_ids

    def test_calendar_week_view(
        self, dashboard_client: Client, dashboard_user: RevelUser, organization: Organization
    ) -> None:
        """Test week view returns user-related events in specified ISO week."""
        # Week 1 of 2026: Dec 29, 2025 - Jan 4, 2026
        week_1_event = Event.objects.create(
            organization=organization,
            name="Week 1 Event",
            slug="week-1",
            status="open",
            start=datetime(2026, 1, 2, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 2, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=week_1_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        week_2_event = Event.objects.create(
            organization=organization,
            name="Week 2 Event",
            slug="week-2",
            status="open",
            start=datetime(2026, 1, 10, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 10, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=week_2_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        url = reverse("api:dashboard_calendar")
        response = dashboard_client.get(url, {"week": "1", "year": "2026"})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(week_1_event.id) in event_ids
        assert str(week_2_event.id) not in event_ids

    def test_calendar_respects_relationship_filters(
        self, dashboard_client: Client, dashboard_user: RevelUser, dashboard_setup: dict[str, t.Any]
    ) -> None:
        """Test that calendar respects DashboardEventsFiltersSchema relationship filters."""
        # Use events from dashboard_setup (all created with start=timezone.now())
        # Update them to be in June 2026
        for event in dashboard_setup["events"].values():
            event.start = datetime(2026, 6, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC"))
            event.end = datetime(2026, 6, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
            event.save()

        url = reverse("api:dashboard_calendar")

        # Get all events (default filters: all true except rsvp_no)
        response = dashboard_client.get(url, {"month": "6", "year": "2026"})
        assert response.status_code == 200
        assert len(response.json()) == 6  # All 6 events from setup

        # Filter to only RSVP'd events
        response = dashboard_client.get(
            url,
            {
                "month": "6",
                "year": "2026",
                "owner": "false",
                "staff": "false",
                "member": "false",
                "rsvp_yes": "true",
                "rsvp_maybe": "false",
                "got_ticket": "false",
                "got_invitation": "false",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "RSVP'd Event"

    def test_calendar_includes_past_events(
        self, dashboard_client: Client, dashboard_user: RevelUser, organization: Organization
    ) -> None:
        """Test that calendar includes past events within the date range (unlike dashboard_events)."""
        # Create past event in June 2026 (past from Nov 2026 perspective)
        past_event = Event.objects.create(
            organization=organization,
            name="Past June Event",
            slug="past-june",
            status="open",
            start=datetime(2026, 6, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=past_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        # Create future event in December 2026
        future_event = Event.objects.create(
            organization=organization,
            name="Future December Event",
            slug="future-dec",
            status="open",
            start=datetime(2026, 12, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 12, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=future_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        url = reverse("api:dashboard_calendar")

        # Request June 2026 (past from Nov 2026 perspective) - should include past event
        response = dashboard_client.get(url, {"month": "6", "year": "2026"})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(past_event.id)

    def test_calendar_filters_draft_events_for_non_staff(
        self, dashboard_client: Client, dashboard_user: RevelUser, organization: Organization
    ) -> None:
        """Test that non-staff users don't see draft events in calendar."""
        # Create draft event
        draft_event = Event.objects.create(
            organization=organization,
            name="Draft Event",
            slug="draft-event",
            status=Event.EventStatus.DRAFT,
            start=datetime(2026, 6, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=draft_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        # Create open event
        open_event = Event.objects.create(
            organization=organization,
            name="Open Event",
            slug="open-event",
            status="open",
            start=datetime(2026, 6, 20, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 20, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=open_event, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        url = reverse("api:dashboard_calendar")
        response = dashboard_client.get(url, {"month": "6", "year": "2026"})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        # Non-staff user should not see draft event
        assert str(draft_event.id) not in event_ids
        assert str(open_event.id) in event_ids
        assert len(data) == 1

    def test_calendar_orders_by_start_time(
        self, dashboard_client: Client, dashboard_user: RevelUser, organization: Organization
    ) -> None:
        """Test that events are ordered by start time ascending."""
        event_late = Event.objects.create(
            organization=organization,
            name="Late Event",
            slug="late-event",
            status="open",
            start=datetime(2026, 6, 20, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 20, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=event_late, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        event_early = Event.objects.create(
            organization=organization,
            name="Early Event",
            slug="early-event",
            status="open",
            start=datetime(2026, 6, 10, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 10, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        EventRSVP.objects.create(event=event_early, user=dashboard_user, status=EventRSVP.RsvpStatus.YES)

        url = reverse("api:dashboard_calendar")
        response = dashboard_client.get(url, {"month": "6", "year": "2026"})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["id"] == str(event_early.id)
        assert data[1]["id"] == str(event_late.id)

    def test_calendar_validation_errors(self, dashboard_client: Client) -> None:
        """Test that invalid parameters return validation errors."""
        url = reverse("api:dashboard_calendar")

        # Invalid week
        response = dashboard_client.get(url, {"week": "54", "year": "2025"})
        assert response.status_code == 422

        # Invalid month
        response = dashboard_client.get(url, {"month": "13", "year": "2025"})
        assert response.status_code == 422

        # Invalid year
        response = dashboard_client.get(url, {"year": "1899"})
        assert response.status_code == 422
