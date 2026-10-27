"""Tests for the optional ``organization`` filter on the ``/me/following`` list endpoints (#1154).

Each endpoint gets a narrowing test (filter by one organization) and a no-filter test
(all active follows returned).
"""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import EventSeries, Organization
from events.models.follow import EventSeriesFollow, OrganizationFollow

pytestmark = pytest.mark.django_db


@pytest.fixture
def follower_setup(revel_user_factory: t.Any) -> dict[str, t.Any]:
    """A user following two organizations, each with one event series they also follow."""
    user: RevelUser = revel_user_factory()
    access_token = RefreshToken.for_user(user).access_token  # type: ignore[attr-defined]
    client = Client(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    orgs: dict[str, Organization] = {}
    series: dict[str, EventSeries] = {}
    for key in ("alpha", "beta"):
        org = Organization.objects.create(
            name=f"{key.title()} Org",
            slug=f"{key}-org",
            owner=revel_user_factory(),
            visibility=Organization.Visibility.PUBLIC,
        )
        orgs[key] = org
        series[key] = EventSeries.objects.create(organization=org, name=f"{key.title()} Series", slug=f"{key}-series")
        OrganizationFollow.objects.create(user=user, organization=org, is_archived=False)
        EventSeriesFollow.objects.create(user=user, event_series=series[key], is_archived=False)
    return {"client": client, "user": user, "orgs": orgs, "series": series}


def test_followed_organizations_organization_filter_narrows(follower_setup: dict[str, t.Any]) -> None:
    url = reverse("api:list_followed_organizations")
    response = follower_setup["client"].get(url, {"organization": str(follower_setup["orgs"]["alpha"].id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["organization"]["id"] == str(follower_setup["orgs"]["alpha"].id)


def test_followed_organizations_no_organization_filter_returns_all(follower_setup: dict[str, t.Any]) -> None:
    response = follower_setup["client"].get(reverse("api:list_followed_organizations"))
    assert response.status_code == 200
    assert response.json()["count"] == 2


def test_followed_event_series_organization_filter_narrows(follower_setup: dict[str, t.Any]) -> None:
    url = reverse("api:list_followed_event_series")
    response = follower_setup["client"].get(url, {"organization": str(follower_setup["orgs"]["beta"].id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["event_series"]["id"] == str(follower_setup["series"]["beta"].id)


def test_followed_event_series_no_organization_filter_returns_all(follower_setup: dict[str, t.Any]) -> None:
    response = follower_setup["client"].get(reverse("api:list_followed_event_series"))
    assert response.status_code == 200
    assert response.json()["count"] == 2
