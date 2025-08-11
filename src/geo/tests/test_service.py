from unittest.mock import MagicMock, patch

import pytest
from django.contrib.gis.geos import Point

from geo.models import City
from geo.service import get_cities_by_ip


@pytest.mark.django_db
@patch("geo.service.resolve_ip_to_point")
def test_get_cities_by_ip(mock_resolve_ip_to_point: MagicMock) -> None:
    """Tests that the get_cities_by_ip function returns cities ordered by distance."""
    # Create some cities
    City.objects.all().delete()
    london = City.objects.create(name="London", city_id=1, location=Point(0.1278, 51.5074, srid=4326))
    paris = City.objects.create(name="Paris", city_id=2, location=Point(2.3522, 48.8566, srid=4326))
    new_york = City.objects.create(name="New York", city_id=3, location=Point(-74.0060, 40.7128, srid=4326))

    # Mock the IP address to resolve to a point close to London
    mock_resolve_ip_to_point.return_value = Point(0.1, 51.5, srid=4326)

    # Get the cities ordered by distance
    cities = get_cities_by_ip("8.8.8.8", max_radius=100000000)
    # Check that the cities are ordered correctly
    city_ids = [city.id for city in cities]
    assert city_ids.index(london.id) < city_ids.index(paris.id)
    assert city_ids.index(paris.id) < city_ids.index(new_york.id)

    # Mock the IP address to resolve to a point close to New York
    mock_resolve_ip_to_point.return_value = Point(-74.1, 40.7, srid=4326)

    # Get the cities ordered by distance
    cities = get_cities_by_ip("8.8.8.8", max_radius=100000000)

    # Check that the cities are ordered correctly
    city_ids = [city.id for city in cities]
    assert city_ids.index(new_york.id) < city_ids.index(london.id)
    assert city_ids.index(london.id) < city_ids.index(paris.id)


@pytest.mark.django_db
@patch("geo.service.resolve_ip_to_point")
def test_get_cities_by_ip_no_point(mock_resolve_ip_to_point: MagicMock) -> None:
    """Tests that the get_cities_by_ip function returns an empty queryset if the IP address cannot be resolved."""
    # Mock the IP address to resolve to None
    mock_resolve_ip_to_point.return_value = None

    # Get the cities ordered by distance
    cities = get_cities_by_ip("127.0.0.1")

    # Check that the whole qs is returned
    assert cities.count() == City.objects.count()
