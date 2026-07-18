"""Tests for the bulk seat painting endpoint (PUT /venues/{venue_id}/seats/paint)."""

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Organization, PriceCategory, Venue, VenueSeat, VenueSector

pytestmark = pytest.mark.django_db


@pytest.fixture
def venue(organization: Organization) -> Venue:
    return Venue.objects.create(organization=organization, name="Theater")


@pytest.fixture
def sector(venue: Venue) -> VenueSector:
    return VenueSector.objects.create(venue=venue, name="Orchestra")


@pytest.fixture
def category(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")


def _url(organization: Organization, venue: Venue) -> str:
    return reverse("api:paint_venue_seats", kwargs={"slug": organization.slug, "venue_id": venue.id})


class TestPaintSeatsEndpoint:
    def test_paint_seats(
        self,
        organization_owner_client: Client,
        organization: Organization,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
    ) -> None:
        seats = [VenueSeat.objects.create(sector=sector, label=f"A{i}") for i in range(1, 4)]
        payload = {"seat_ids": [str(s.id) for s in seats[:2]], "price_category_id": str(category.id)}

        response = organization_owner_client.put(
            _url(organization, venue), data=orjson.dumps(payload), content_type="application/json"
        )

        assert response.status_code == 200, response.content
        assert response.json() == {"painted": 2}
        assert VenueSeat.objects.filter(default_price_category=category).count() == 2

    def test_unpaint_with_null(
        self,
        organization_owner_client: Client,
        organization: Organization,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
    ) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        payload = {"seat_ids": [str(seat.id)], "price_category_id": None}

        response = organization_owner_client.put(
            _url(organization, venue), data=orjson.dumps(payload), content_type="application/json"
        )

        assert response.status_code == 200, response.content
        assert response.json() == {"painted": 1}
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    def test_foreign_category_rejected(
        self,
        organization_owner_client: Client,
        organization: Organization,
        venue: Venue,
        sector: VenueSector,
    ) -> None:
        other = Venue.objects.create(organization=organization, name="Other Hall")
        foreign_category = PriceCategory.objects.create(venue=other, name="Foreign", color="#00aa00")
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        payload = {"seat_ids": [str(seat.id)], "price_category_id": str(foreign_category.id)}

        response = organization_owner_client.put(
            _url(organization, venue), data=orjson.dumps(payload), content_type="application/json"
        )

        assert response.status_code == 400, response.content
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    def test_foreign_seats_rejected(
        self,
        organization_owner_client: Client,
        organization: Organization,
        venue: Venue,
        category: PriceCategory,
    ) -> None:
        other = Venue.objects.create(organization=organization, name="Other Hall")
        other_sector = VenueSector.objects.create(venue=other, name="Foreign")
        foreign_seat = VenueSeat.objects.create(sector=other_sector, label="X1")
        payload = {"seat_ids": [str(foreign_seat.id)], "price_category_id": str(category.id)}

        response = organization_owner_client.put(
            _url(organization, venue), data=orjson.dumps(payload), content_type="application/json"
        )

        assert response.status_code == 404, response.content
        foreign_seat.refresh_from_db()
        assert foreign_seat.default_price_category_id is None

    def test_nonmember_gets_403(
        self,
        nonmember_client: Client,
        organization: Organization,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
    ) -> None:
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        payload = {"seat_ids": [str(seat.id)], "price_category_id": str(category.id)}

        response = nonmember_client.put(
            _url(organization, venue), data=orjson.dumps(payload), content_type="application/json"
        )

        assert response.status_code in (403, 404)
        seat.refresh_from_db()
        assert seat.default_price_category_id is None
