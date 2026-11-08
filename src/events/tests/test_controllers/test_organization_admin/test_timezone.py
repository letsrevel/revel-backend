"""The organization's resolved IANA timezone is exposed on the admin detail and public retrieve schemas."""

import typing as t

import pytest
from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from events.models import Organization
from events.utils import get_organization_timezone
from geo.models import City

pytestmark = pytest.mark.django_db


@pytest.fixture
def tokyo_organization(organization: Organization) -> Organization:
    """The shared organization, moved to a Tokyo city."""
    organization.city = City.objects.create(
        name="Tokyo",
        ascii_name="Tokyo",
        country="Japan",
        iso2="JP",
        iso3="JPN",
        city_id=88002,
        location=Point(139.69, 35.68, srid=4326),
        timezone="Asia/Tokyo",
    )
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save()
    return organization


def _admin_url(org: Organization) -> str:
    return reverse("api:get_organization_admin", kwargs={"slug": org.slug})


def _public_url(org: Organization) -> str:
    return reverse("api:get_organization", kwargs={"slug": org.slug})


def test_admin_detail_returns_city_timezone(
    organization_owner_client: Client, tokyo_organization: Organization
) -> None:
    response = organization_owner_client.get(_admin_url(tokyo_organization))

    assert response.status_code == 200
    assert response.json()["timezone"] == "Asia/Tokyo"
    assert response.json()["timezone"] == get_organization_timezone(tokyo_organization).key


def test_admin_detail_falls_back_to_platform_timezone(
    organization_owner_client: Client, organization: Organization
) -> None:
    assert organization.city is None

    response = organization_owner_client.get(_admin_url(organization))

    assert response.status_code == 200
    assert response.json()["timezone"] == settings.TIME_ZONE
    assert response.json()["timezone"] == get_organization_timezone(organization).key


def test_public_retrieve_returns_city_timezone(client: Client, tokyo_organization: Organization) -> None:
    response = client.get(_public_url(tokyo_organization))

    assert response.status_code == 200
    assert response.json()["timezone"] == "Asia/Tokyo"


def test_public_retrieve_falls_back_to_platform_timezone(client: Client, organization: Organization) -> None:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save()

    response = client.get(_public_url(organization))

    assert response.status_code == 200
    assert response.json()["timezone"] == settings.TIME_ZONE


@pytest.mark.parametrize("endpoint", [_admin_url, _public_url])
def test_timezone_adds_no_city_queries(
    organization_owner_client: Client, tokyo_organization: Organization, endpoint: t.Callable[[Organization], str]
) -> None:
    """The city is already loaded for the ``city`` field; resolving the timezone must not re-query it."""
    with CaptureQueriesContext(connection) as captured:
        response = organization_owner_client.get(endpoint(tokyo_organization))

    assert response.status_code == 200
    city_queries = [q for q in captured.captured_queries if "geo_city" in q["sql"]]
    assert len(city_queries) <= 1
