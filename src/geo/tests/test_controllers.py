import pytest
from django.contrib.gis.geos import Point
from django.test import Client
from django.urls import reverse

from geo.models import City


@pytest.mark.django_db
def test_list_cities(client: Client) -> None:
    """Tests that the list_cities endpoint returns a list of cities."""
    City.objects.create(
        name="London",
        ascii_name="London",
        country="GB",
        city_id=1,
        location=Point(0.1278, 51.5074),
    )
    City.objects.create(
        name="Paris",
        ascii_name="Paris",
        country="FR",
        city_id=2,
        location=Point(2.3522, 48.8566),
    )

    url = reverse("api:list_cities")
    response = client.get(url)

    assert response.status_code == 200
    assert len(response.json()["results"])


@pytest.mark.django_db
def test_get_city(client: Client) -> None:
    """Tests that the get_city endpoint returns a single city."""
    city = City.objects.create(
        name="London",
        ascii_name="London",
        country="GB",
        city_id=1,
        location=Point(0.1278, 51.5074),
    )

    url = reverse("api:get_city", kwargs={"city_id": city.city_id})
    response = client.get(url)

    assert response.status_code == 200
    assert response.json()["name"] == "London"
