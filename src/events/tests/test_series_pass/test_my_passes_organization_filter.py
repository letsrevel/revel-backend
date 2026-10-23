"""Tests for the optional ``organization`` filter on GET /series-passes/me (#1154)."""

from decimal import Decimal

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    EventSeries,
    HeldSeriesPass,
    Organization,
    SeriesPass,
    TicketTier,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def revel_user_client(revel_user: RevelUser) -> Client:
    """API client for the series-pass holder."""
    refresh = RefreshToken.for_user(revel_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def other_org_pass(organization: Organization, revel_user: RevelUser) -> HeldSeriesPass:
    """A pass held by ``revel_user`` for a series in a second organization."""
    other_org = Organization.objects.create(name="Other Org", owner=RevelUser.objects.create_user("other-org-owner"))
    other_series = EventSeries.objects.create(organization=other_org, name="Other Series", slug="other-series")
    other_pass = SeriesPass.objects.create(
        event_series=other_series,
        name="Other Pass",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("2.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    return HeldSeriesPass.objects.create(
        series_pass=other_pass,
        user=revel_user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=other_pass.price,
    )


@pytest.fixture
def own_org_pass(revel_user: RevelUser, series_pass: SeriesPass) -> HeldSeriesPass:
    """A pass held by ``revel_user`` for the series in the test organization."""
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=revel_user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=series_pass.price,
    )


def test_list_my_series_passes_organization_filter_narrows(
    revel_user_client: Client,
    organization: Organization,
    own_org_pass: HeldSeriesPass,
    other_org_pass: HeldSeriesPass,
) -> None:
    url = reverse("api:list_my_series_passes")
    response = revel_user_client.get(url, {"organization": str(organization.id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(own_org_pass.id)


def test_list_my_series_passes_no_organization_filter_returns_all(
    revel_user_client: Client,
    own_org_pass: HeldSeriesPass,
    other_org_pass: HeldSeriesPass,
) -> None:
    response = revel_user_client.get(reverse("api:list_my_series_passes"))
    assert response.status_code == 200
    assert response.json()["count"] == 2
