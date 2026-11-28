"""Tests for GET /events/calendar endpoint."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import Point
from django.test.client import Client
from django.urls import reverse
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import (
    Event,
    Organization,
)
from geo.models import City

pytestmark = pytest.mark.django_db


class TestCalendarEndpoint:
    """Tests for the calendar endpoint."""

    def test_calendar_default_returns_current_month_events(self, client: Client, organization: Organization) -> None:
        """Test that calling /calendar with no params defaults to current month (Dec 2025)."""
        # Use December 2025 since we're currently in Nov 2025
        this_month_event = Event.objects.create(
            organization=organization,
            name="December Event",
            slug="dec-event-default",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2025, 12, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2025, 12, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        next_year_event = Event.objects.create(
            organization=organization,
            name="January 2026 Event",
            slug="jan-2026-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 1, 20, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 20, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        # Will default to current month (December 2025)
        with freeze_time("2025-12-01"):
            response = client.get(url)

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(this_month_event.id) in event_ids
        assert str(next_year_event.id) not in event_ids

    def test_calendar_month_view(self, client: Client, organization: Organization) -> None:
        """Test month view returns only events in specified month."""
        # Ensure organization is visible to anonymous users
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # Create events in different months (using future dates to avoid past event filtering)
        dec_event = Event.objects.create(
            organization=organization,
            name="December Event",
            slug="dec-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 12, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 12, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        jan_event = Event.objects.create(
            organization=organization,
            name="January Event",
            slug="jan-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2027, 1, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2027, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"month": 12, "year": 2026})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(dec_event.id) in event_ids
        assert str(jan_event.id) not in event_ids

    def test_calendar_year_view(self, client: Client, organization: Organization) -> None:
        """Test year view returns all events in that year."""
        # Create events in different years
        event_2026 = Event.objects.create(
            organization=organization,
            name="2026 Event",
            slug="event-2026",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        event_2027 = Event.objects.create(
            organization=organization,
            name="2027 Event",
            slug="event-2027",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2027, 1, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2027, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"year": 2026})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(event_2026.id) in event_ids
        assert str(event_2027.id) not in event_ids

    def test_calendar_year_view_matches_events_on_their_local_day(
        self, client: Client, organization: Organization
    ) -> None:
        """A year holds the events whose start date in the event's own timezone is in that year (#1520).

        UTC offsets run from -12 to +14 hours, so the UTC bounds of the year are wrong in both directions.
        """
        city_ids = iter(range(911_000, 911_100))

        def city(tz: str) -> City:
            return City.objects.create(
                name=tz,
                ascii_name=tz,
                country="Test",
                iso2="TT",
                iso3="TST",
                city_id=next(city_ids),
                location=Point(0, 0, srid=4326),
                timezone=tz,
            )

        def make(slug: str, start: datetime, in_city: City | None = None) -> Event:
            return Event.objects.create(
                organization=organization,
                name=slug,
                slug=slug,
                visibility=Event.Visibility.PUBLIC,
                event_type=Event.EventType.PUBLIC,
                status="open",
                requires_ticket=False,
                start=start,
                city=in_city,
            )

        vienna, kiritimati, gmt_minus_12 = city("Europe/Vienna"), city("Pacific/Kiritimati"), city("Etc/GMT+12")
        # Local 2027 dates whose UTC start is in 2026 or 2028.
        included = {
            make("vienna-new-year", datetime(2027, 1, 1, 0, 30, tzinfo=ZoneInfo("Europe/Vienna")), vienna),
            make("kiritimati-new-year", datetime(2027, 1, 1, 0, 30, tzinfo=ZoneInfo("Pacific/Kiritimati")), kiritimati),
            make("last-evening-utc-12", datetime(2027, 12, 31, 23, 30, tzinfo=ZoneInfo("Etc/GMT+12")), gmt_minus_12),
        }
        # UTC 2027 starts whose local date is in 2026 or 2028; and no city (UTC fallback) on 2026-12-31.
        excluded = {
            make("vienna-next-new-year", datetime(2028, 1, 1, 0, 30, tzinfo=ZoneInfo("Europe/Vienna")), vienna),
            make("utc-12-eve", datetime(2026, 12, 31, 23, 30, tzinfo=ZoneInfo("Etc/GMT+12")), gmt_minus_12),
            make("no-city-eve", datetime(2026, 12, 31, 23, 30, tzinfo=ZoneInfo("UTC"))),
        }

        response = client.get(reverse("api:calendar_events"), {"year": 2027})

        assert response.status_code == 200
        event_ids = {e["id"] for e in response.json()}
        assert {str(e.id) for e in included} <= event_ids
        assert not {str(e.id) for e in excluded} & event_ids

    def test_calendar_week_view(self, client: Client, organization: Organization) -> None:
        """Test week view returns events in specified ISO week."""
        # Week 1 of 2026: Dec 29, 2025 - Jan 4, 2026
        week_1_event = Event.objects.create(
            organization=organization,
            name="Week 1 Event",
            slug="week-1",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 1, 2, 10, 0, 0, tzinfo=ZoneInfo("UTC")),  # Friday of Week 1
            end=datetime(2026, 1, 2, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        week_2_event = Event.objects.create(
            organization=organization,
            name="Week 2 Event",
            slug="week-2",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 1, 7, 10, 0, 0, tzinfo=ZoneInfo("UTC")),  # Wednesday of Week 2
            end=datetime(2026, 1, 7, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"week": 1, "year": 2026})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(week_1_event.id) in event_ids
        assert str(week_2_event.id) not in event_ids

    def test_calendar_respects_event_filter_schema(
        self, client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that EventFilterSchema parameters work with calendar."""
        other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
        org_event = Event.objects.create(
            organization=organization,
            name="Org Event",
            slug="org-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        other_org_event = Event.objects.create(
            organization=other_org,
            name="Other Org Event",
            slug="other-org-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 20, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 20, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"month": "6", "year": "2026", "organization": str(organization.id)})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(org_event.id) in event_ids
        assert str(other_org_event.id) not in event_ids

    def test_calendar_orders_by_start_time(self, client: Client, organization: Organization) -> None:
        """Test that events are ordered by start time ascending."""
        event_late = Event.objects.create(
            organization=organization,
            name="Late Event",
            slug="late-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 20, 15, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 20, 17, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        event_early = Event.objects.create(
            organization=organization,
            name="Early Event",
            slug="early-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 10, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 10, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"month": 6, "year": 2026})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["id"] == str(event_early.id)
        assert data[1]["id"] == str(event_late.id)

    @pytest.mark.parametrize("year, expected_status", [(2025, 422), (2026, 200)])
    def test_calendar_iso_week_53(self, client: Client, year: int, expected_status: int) -> None:
        """Validate week 53 against the number of ISO weeks in the year."""
        response = client.get(reverse("api:calendar_events"), {"week": 53, "year": year})
        assert response.status_code == expected_status

    def test_calendar_invalid_week_number(self, client: Client) -> None:
        """Test that invalid week number returns validation error."""
        url = reverse("api:calendar_events")

        # Test week = 0
        response = client.get(url, {"week": "0", "year": "2025"})
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

        # Test week = 54
        response = client.get(url, {"week": "54", "year": "2025"})
        assert response.status_code == 422

        # Test negative week
        response = client.get(url, {"week": "-1", "year": "2025"})
        assert response.status_code == 422

    def test_calendar_invalid_month_number(self, client: Client) -> None:
        """Test that invalid month number returns validation error."""
        url = reverse("api:calendar_events")

        # Test month = 0
        response = client.get(url, {"month": "0", "year": "2025"})
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

        # Test month = 13
        response = client.get(url, {"month": "13", "year": "2025"})
        assert response.status_code == 422

        # Test negative month
        response = client.get(url, {"month": "-1", "year": "2025"})
        assert response.status_code == 422

    def test_calendar_invalid_year(self, client: Client) -> None:
        """Test that invalid year returns validation error."""
        url = reverse("api:calendar_events")

        # Test year = 0
        response = client.get(url, {"year": "0"})
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

        # Test year too far in past
        response = client.get(url, {"year": "1899"})
        assert response.status_code == 422

        # Test year too far in future
        response = client.get(url, {"year": "3001"})
        assert response.status_code == 422

        # Test negative year
        response = client.get(url, {"year": "-2025"})
        assert response.status_code == 422
