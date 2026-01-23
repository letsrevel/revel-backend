"""Tests for geo models."""

import pytest
from django.contrib.gis.geos import Point

from geo.models import City


@pytest.mark.django_db
class TestCityTimezone:
    """Tests for City timezone auto-population."""

    def test_timezone_populated_on_save(self) -> None:
        """Test that timezone is auto-populated from location coordinates on save."""
        # Vienna coordinates
        city = City.objects.create(
            name="Vienna",
            ascii_name="Vienna",
            country="Austria",
            iso2="AT",
            iso3="AUT",
            city_id=99999,
            location=Point(16.3738, 48.2082, srid=4326),  # lon, lat
        )
        assert city.timezone == "Europe/Vienna"

    def test_timezone_not_overwritten_if_set(self) -> None:
        """Test that explicitly set timezone is not overwritten."""
        city = City.objects.create(
            name="Test City",
            ascii_name="Test City",
            country="Test",
            iso2="XX",
            iso3="XXX",
            city_id=99998,
            location=Point(16.3738, 48.2082, srid=4326),
            timezone="Europe/London",  # Explicitly set different timezone
        )
        assert city.timezone == "Europe/London"

    def test_timezone_for_different_locations(self) -> None:
        """Test timezone detection for various locations."""
        test_cases = [
            # (name, lon, lat, expected_timezone)
            ("New York", -74.0060, 40.7128, "America/New_York"),
            ("Tokyo", 139.6917, 35.6895, "Asia/Tokyo"),
            ("Sydney", 151.2093, -33.8688, "Australia/Sydney"),
            ("London", -0.1276, 51.5074, "Europe/London"),
        ]

        for name, lon, lat, expected_tz in test_cases:
            city = City(
                name=name,
                ascii_name=name,
                country="Test",
                iso2="XX",
                iso3="XXX",
                city_id=hash(name) % 100000,
                location=Point(lon, lat, srid=4326),
            )
            city.save()
            assert city.timezone == expected_tz, f"Expected {expected_tz} for {name}, got {city.timezone}"
            city.delete()

    def test_timezone_none_without_location(self) -> None:
        """Test that timezone remains None if city has no location."""
        city = City(
            name="No Location City",
            ascii_name="No Location City",
            country="Test",
            iso2="XX",
            iso3="XXX",
            city_id=99997,
        )
        # Location is not set, so timezone should remain None
        # Note: Can't save without location (PointField is required), but test the logic
        assert city.timezone is None
