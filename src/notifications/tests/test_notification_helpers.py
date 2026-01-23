"""Tests for notification helper functions."""

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.gis.geos import Point

from geo.models import City
from notifications.service.notification_helpers import format_event_datetime, get_event_timezone


@pytest.fixture
def vienna_city(db: None) -> City:
    """Create a Vienna city with timezone."""
    return City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="Austria",
        iso2="AT",
        iso3="AUT",
        city_id=99999,
        location=Point(16.3738, 48.2082, srid=4326),
        timezone="Europe/Vienna",
    )


@pytest.fixture
def new_york_city(db: None) -> City:
    """Create a New York city with timezone."""
    return City.objects.create(
        name="New York",
        ascii_name="New York",
        country="United States",
        iso2="US",
        iso3="USA",
        city_id=99998,
        location=Point(-74.0060, 40.7128, srid=4326),
        timezone="America/New_York",
    )


def _create_mock_event(city: City | None) -> MagicMock:
    """Create a mock event with the given city."""
    event = MagicMock()
    event.city = city
    return event


class TestGetEventTimezone:
    """Tests for get_event_timezone function."""

    def test_returns_city_timezone_when_available(self, vienna_city: City) -> None:
        """Test that city timezone is returned when event has a city."""
        event = _create_mock_event(vienna_city)

        result = get_event_timezone(event)

        assert result == ZoneInfo("Europe/Vienna")

    def test_returns_utc_when_no_city(self) -> None:
        """Test that UTC is returned when event has no city."""
        event = _create_mock_event(None)

        result = get_event_timezone(event)

        assert result == ZoneInfo("UTC")

    def test_returns_utc_when_city_has_no_timezone(self) -> None:
        """Test that UTC is returned when city has no timezone set."""
        city_mock = MagicMock()
        city_mock.timezone = None
        event = _create_mock_event(city_mock)

        result = get_event_timezone(event)

        assert result == ZoneInfo("UTC")

    def test_returns_utc_for_invalid_timezone(self) -> None:
        """Test that UTC is returned for invalid timezone string."""
        city_mock = MagicMock()
        city_mock.id = 1
        city_mock.timezone = "Invalid/Timezone"
        event = _create_mock_event(city_mock)

        result = get_event_timezone(event)

        assert result == ZoneInfo("UTC")


class TestFormatEventDatetime:
    """Tests for format_event_datetime function."""

    def test_formats_datetime_in_event_timezone(self, vienna_city: City) -> None:
        """Test that datetime is formatted in the event's timezone."""
        event = _create_mock_event(vienna_city)

        # Create a datetime in UTC
        dt = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = format_event_datetime(dt, event)

        # Vienna is UTC+1 in winter, so 18:00 UTC = 19:00 CET
        assert "7:00 PM" in result or "19:00" in result
        assert "CET" in result or "Central European" in result

    def test_returns_empty_string_for_none(self, vienna_city: City) -> None:
        """Test that empty string is returned for None datetime."""
        event = _create_mock_event(vienna_city)

        result = format_event_datetime(None, event)

        assert result == ""

    def test_formats_in_utc_when_no_city(self) -> None:
        """Test that datetime is formatted in UTC when event has no city."""
        event = _create_mock_event(None)

        dt = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = format_event_datetime(dt, event)

        assert "6:00 PM" in result or "18:00" in result
        assert "UTC" in result

    def test_different_timezones_produce_different_output(self, vienna_city: City, new_york_city: City) -> None:
        """Test that different city timezones produce different formatted output."""
        event_vienna = _create_mock_event(vienna_city)
        event_ny = _create_mock_event(new_york_city)

        # Same UTC time
        dt = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

        result_vienna = format_event_datetime(dt, event_vienna)
        result_ny = format_event_datetime(dt, event_ny)

        # Vienna: 18:00 UTC = 19:00 CET (UTC+1)
        # New York: 18:00 UTC = 13:00 EST (UTC-5)
        assert result_vienna != result_ny
        # Vienna should show 7:00 PM (19:00)
        assert "7:00 PM" in result_vienna or "19:00" in result_vienna
        # New York should show 1:00 PM (13:00)
        assert "1:00 PM" in result_ny or "13:00" in result_ny

    def test_custom_format_string(self, vienna_city: City) -> None:
        """Test that custom format string is applied."""
        event = _create_mock_event(vienna_city)

        dt = datetime(2026, 2, 6, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

        result = format_event_datetime(dt, event, fmt="Y-m-d H:i")

        # Should use custom format
        assert "2026-02-06" in result
        assert "19:00" in result
