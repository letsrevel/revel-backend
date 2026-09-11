import pytest
from django.contrib.gis.geos import Point
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
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
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url, {"search": "Lon", "country": "GB"})

    assert response.status_code == 200
    assert [c["name"] for c in response.json()["results"]] == ["London"]
    # City has no relations and the filter/search touch scalar columns only, so no
    # row can be duplicated: DISTINCT would just force a Unique-over-Sort before LIMIT.
    city_queries = [q["sql"] for q in ctx.captured_queries if '"geo_city"' in q["sql"]]
    assert city_queries
    assert not any("DISTINCT" in sql for sql in city_queries)


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

    url = reverse("api:get_city", kwargs={"city_id": city.pk})
    response = client.get(url)

    assert response.status_code == 200
    assert response.json()["name"] == "London"


@pytest.mark.django_db
def test_get_city_looks_up_by_pk_not_external_city_id(client: Client) -> None:
    """The detail route must accept the ``id`` exposed by ``CitySchema`` (the pk).

    ``City.city_id`` is the external dataset id; the list endpoint never returns it,
    so a client following ``id`` from the list would 404 whenever pk != city_id.
    """
    city = City.objects.create(
        name="Vienna",
        ascii_name="Vienna",
        country="AT",
        city_id=987_654_321,
        location=Point(16.3738, 48.2082),
    )
    assert city.pk != city.city_id

    response = client.get(reverse("api:get_city", kwargs={"city_id": city.pk}))
    assert response.status_code == 200
    assert response.json()["id"] == city.pk

    assert not City.objects.filter(pk=city.city_id).exists()
    response = client.get(reverse("api:get_city", kwargs={"city_id": city.city_id}))
    assert response.status_code == 404
