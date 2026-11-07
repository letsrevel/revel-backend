"""Fixtures for the member register tests."""

import pytest
from django.contrib.gis.geos import Point

from events.models import Organization
from geo.models import City


@pytest.fixture
def ny_organization(organization: Organization) -> Organization:
    """The shared organization, moved to a New York city (America/New_York timezone)."""
    city = City.objects.create(
        name="New York",
        ascii_name="New York",
        country="United States",
        iso2="US",
        iso3="USA",
        city_id=88001,
        location=Point(-74.0060, 40.7128, srid=4326),
        timezone="America/New_York",
    )
    organization.city = city
    organization.save(update_fields=["city"])
    return organization
