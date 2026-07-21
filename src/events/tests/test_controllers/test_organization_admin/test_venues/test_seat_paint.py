"""Tests for the bulk seat painting endpoint (PUT /venues/{venue_id}/seats/paint)."""

from decimal import Decimal

import orjson
import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from events.models import Event, Organization, PriceCategory, TicketTier, Venue, VenueSeat, VenueSector

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
        assert response.json() == {"painted": 2, "affected_tiers": [], "unsellable_zone_tiers": []}
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
        assert response.json() == {"painted": 1, "affected_tiers": [], "unsellable_zone_tiers": []}
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

    def test_painted_seat_round_trips_category_on_sector_read(
        self,
        organization_owner_client: Client,
        organization: Organization,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
    ) -> None:
        """After painting, the admin sector-detail read serializes the seat's category (#733)."""
        painted = VenueSeat.objects.create(sector=sector, label="A1")
        VenueSeat.objects.create(sector=sector, label="A2")  # left unpainted

        paint = organization_owner_client.put(
            _url(organization, venue),
            data=orjson.dumps({"seat_ids": [str(painted.id)], "price_category_id": str(category.id)}),
            content_type="application/json",
        )
        assert paint.status_code == 200, paint.content

        detail_url = reverse(
            "api:get_venue_sector",
            kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
        )
        detail = organization_owner_client.get(detail_url)
        assert detail.status_code == 200, detail.content
        seats = {s["label"]: s for s in detail.json()["seats"]}

        assert seats["A1"]["price_category_id"] == str(category.id)
        assert seats["A1"]["price_category"]["name"] == category.name
        assert seats["A1"]["price_category"]["color"] == category.color

        assert seats["A2"]["price_category_id"] is None
        assert seats["A2"]["price_category"] is None

    def test_sector_read_category_is_not_n_plus_one(
        self,
        organization_owner_client: Client,
        organization: Organization,
    ) -> None:
        """The sector list query count is constant regardless of painted-seat count (#733)."""

        def count_queries_for(n_seats: int) -> int:
            venue = Venue.objects.create(organization=organization, name=f"Hall {n_seats}")
            sector = VenueSector.objects.create(venue=venue, name="Main")
            cat = PriceCategory.objects.create(venue=venue, name="P", color="#123456")
            VenueSeat.objects.bulk_create(
                [VenueSeat(sector=sector, label=f"S{i}", default_price_category=cat) for i in range(n_seats)]
            )
            url = reverse("api:list_venue_sectors", kwargs={"slug": organization.slug, "venue_id": venue.id})
            with CaptureQueriesContext(connection) as ctx:
                response = organization_owner_client.get(url)
                assert response.status_code == 200, response.content
            return len(ctx.captured_queries)

        assert count_queries_for(2) == count_queries_for(12)

    def test_response_reports_the_tier_it_repriced_and_under_covered(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
    ) -> None:
        """The advisory the grid editor renders after a paint (#746, #747)."""
        event.venue = venue
        event.save(update_fields=["venue"])
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        tier = TicketTier.objects.create(
            event=event,
            name="Stalls",
            price=Decimal("50.00"),
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            venue=venue,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            category_prices={str(category.id): "80.00"},
        )
        balcony = PriceCategory.objects.create(venue=venue, name="Balcony", color="#00aa00")

        response = organization_owner_client.put(
            _url(organization, venue),
            data=orjson.dumps({"seat_ids": [str(seat.id)], "price_category_id": str(balcony.id)}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert response.json() == {
            "painted": 1,
            "affected_tiers": [
                {
                    "tier_id": str(tier.id),
                    "tier_name": "Stalls",
                    "event_id": str(event.id),
                    "event_name": event.name,
                    "event_status": event.status,
                    # An 80.00 seat now has no price at all on this tier: checkout refuses it.
                    "price_changes": [{"seat_count": 1, "from_price": "80.00", "to_price": None}],
                    "missing_categories": [{"id": str(balcony.id), "name": "Balcony", "color": "#00aa00"}],
                }
            ],
            "unsellable_zone_tiers": [],
        }

    def test_response_reports_a_repricing_with_no_coverage_gap(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
    ) -> None:
        """The silent case (#747): both categories priced, so every other signal stays quiet."""
        event.venue = venue
        event.save(update_fields=["venue"])
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        standard = PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa")
        tier = TicketTier.objects.create(
            event=event,
            name="Stalls",
            price=Decimal("50.00"),
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            venue=venue,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            category_prices={str(category.id): "80.00", str(standard.id): "30.00"},
        )

        response = organization_owner_client.put(
            _url(organization, venue),
            data=orjson.dumps({"seat_ids": [str(seat.id)], "price_category_id": str(standard.id)}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert response.json() == {
            "painted": 1,
            "affected_tiers": [
                {
                    "tier_id": str(tier.id),
                    "tier_name": "Stalls",
                    "event_id": str(event.id),
                    "event_name": event.name,
                    "event_status": event.status,
                    "price_changes": [{"seat_count": 1, "from_price": "80.00", "to_price": "30.00"}],
                    "missing_categories": [],
                }
            ],
            "unsellable_zone_tiers": [],
        }

    def test_response_reports_a_zone_the_unpaint_left_unfillable(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
    ) -> None:
        """The converse advisory: the unpaint took the last seat of a zone the tier sells.

        The organizer is on the venue screen at this moment, so this is where they have to
        be told — the tier screen's ``unsellable_zones`` only shows if they think to look.
        """
        event.venue = venue
        event.save(update_fields=["venue"])
        standard = PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa")
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        VenueSeat.objects.create(sector=sector, label="A2", default_price_category=standard)
        tier = TicketTier.objects.create(
            event=event,
            name="Stalls",
            price=Decimal("50.00"),
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            venue=venue,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
            category_prices={str(category.id): "80.00", str(standard.id): "30.00"},
        )

        response = organization_owner_client.put(
            _url(organization, venue),
            data=orjson.dumps({"seat_ids": [str(seat.id)], "price_category_id": None}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert response.json()["unsellable_zone_tiers"] == [
            {
                "tier_id": str(tier.id),
                "tier_name": "Stalls",
                "event_id": str(event.id),
                "event_name": event.name,
                "event_status": event.status,
                "zones": [{"id": str(category.id), "name": "Premium", "color": "#aa0000"}],
            }
        ]

    def test_preview_returns_the_same_body_as_the_paint_and_writes_nothing(
        self,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
    ) -> None:
        """``?preview=true``: the admin sees the repricing before the money moves (#747)."""
        event.venue = venue
        event.save(update_fields=["venue"])
        seat = VenueSeat.objects.create(sector=sector, label="A1", default_price_category=category)
        standard = PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa")
        TicketTier.objects.create(
            event=event,
            name="Stalls",
            price=Decimal("50.00"),
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            venue=venue,
            sector=sector,
            seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
            category_prices={str(category.id): "80.00", str(standard.id): "30.00"},
        )
        body = orjson.dumps({"seat_ids": [str(seat.id)], "price_category_id": str(standard.id)})
        before = VenueSeat.objects.values_list("id", "default_price_category_id", "updated_at").get()

        preview = organization_owner_client.put(
            f"{_url(organization, venue)}?preview=true", data=body, content_type="application/json"
        )

        assert preview.status_code == 200, preview.content
        assert VenueSeat.objects.values_list("id", "default_price_category_id", "updated_at").get() == before

        real = organization_owner_client.put(_url(organization, venue), data=body, content_type="application/json")

        assert real.status_code == 200, real.content
        assert preview.json() == real.json()
        assert preview.json()["affected_tiers"][0]["price_changes"] == [
            {"seat_count": 1, "from_price": "80.00", "to_price": "30.00"}
        ]
        seat.refresh_from_db()
        assert seat.default_price_category_id == standard.id

    @pytest.mark.parametrize("preview", ["true", "false"])
    def test_preview_rejects_a_foreign_seat_exactly_like_the_paint(
        self,
        organization_owner_client: Client,
        organization: Organization,
        venue: Venue,
        category: PriceCategory,
        preview: str,
    ) -> None:
        """A preview of a paint that would 404 must 404 — confirming an impossible paint is worse."""
        other = Venue.objects.create(organization=organization, name="Other Hall")
        other_sector = VenueSector.objects.create(venue=other, name="Foreign")
        foreign_seat = VenueSeat.objects.create(sector=other_sector, label="X1")
        payload = {"seat_ids": [str(foreign_seat.id)], "price_category_id": str(category.id)}

        response = organization_owner_client.put(
            f"{_url(organization, venue)}?preview={preview}",
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 404, response.content
        foreign_seat.refresh_from_db()
        assert foreign_seat.default_price_category_id is None

    @pytest.mark.parametrize("preview", ["true", "false"])
    def test_preview_rejects_a_foreign_category_exactly_like_the_paint(
        self,
        organization_owner_client: Client,
        organization: Organization,
        venue: Venue,
        sector: VenueSector,
        preview: str,
    ) -> None:
        other = Venue.objects.create(organization=organization, name="Other Hall")
        foreign_category = PriceCategory.objects.create(venue=other, name="Foreign", color="#00aa00")
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        payload = {"seat_ids": [str(seat.id)], "price_category_id": str(foreign_category.id)}

        response = organization_owner_client.put(
            f"{_url(organization, venue)}?preview={preview}",
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 400, response.content
        seat.refresh_from_db()
        assert seat.default_price_category_id is None

    def test_preview_needs_the_same_permission_as_the_paint(
        self,
        nonmember_client: Client,
        organization: Organization,
        venue: Venue,
        sector: VenueSector,
        category: PriceCategory,
    ) -> None:
        """The dry run reads pricing across every event at the venue: same gate, no shortcut."""
        seat = VenueSeat.objects.create(sector=sector, label="A1")
        payload = {"seat_ids": [str(seat.id)], "price_category_id": str(category.id)}

        response = nonmember_client.put(
            f"{_url(organization, venue)}?preview=true",
            data=orjson.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code in (403, 404)

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
