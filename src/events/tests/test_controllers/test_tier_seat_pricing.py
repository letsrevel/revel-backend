"""Buyer-facing tier payload: server-resolved category prices (spec §7).

The contract these tests defend is that the frontend never has to reimplement the
fallback chain. Whatever ``resolve_seat_price`` would charge for a seat, the tier
payload must already say — otherwise displayed price and charged price drift.
"""

import typing as t
from decimal import Decimal

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from ninja.errors import HttpError

from events.models import Event, PriceCategory, TicketTier, VenueSeat
from events.service.seating.pricing import resolve_seat_price
from events.utils.tier_pricing import parse_price_map

pytestmark = pytest.mark.django_db


def _tier(event: Event, seats: list[VenueSeat], category_prices: dict[str, str], name: str = "Stalls") -> TicketTier:
    """Create a user-choice tier on the seated event's only sector."""
    venue = event.venue
    assert venue is not None
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("50.00"),
        venue=venue,
        sector=seats[0].sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
        category_prices=category_prices,
    )


def _paint(seats: list[VenueSeat], category: PriceCategory) -> None:
    for seat in seats:
        seat.default_price_category = category
        seat.save(update_fields=["default_price_category"])


def _tier_payload(client: Client, event: Event, tier: TicketTier) -> dict[str, t.Any]:
    resp = client.get(f"/api/events/{event.id}/tickets/tiers")
    assert resp.status_code == 200, resp.content
    tiers = {t_["id"]: t_ for t_ in resp.json()}
    return t.cast(dict[str, t.Any], tiers[str(tier.id)])


def test_flat_tier_has_no_seat_pricing(client: Client, seated_event: tuple[Event, list[VenueSeat]]) -> None:
    """An empty map means "flat tier" — the null is what tells the FE not to render a legend."""
    event, seats = seated_event
    tier = _tier(event, seats, {})
    assert _tier_payload(client, event, tier)["seat_pricing"] is None


def test_category_priced_tier_resolves_every_painted_category(
    client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    event, seats = seated_event
    venue = event.venue
    assert venue is not None
    premium = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000", display_order=0)
    standard = PriceCategory.objects.create(venue=venue, name="Standard", color="#00aa00", display_order=1)
    _paint(seats[:2], premium)
    _paint(seats[2:4], standard)
    tier = _tier(event, seats, {str(premium.id): "80.00", str(standard.id): "40.00"})

    pricing = _tier_payload(client, event, tier)["seat_pricing"]
    assert pricing == {
        "categories": [
            {"id": str(premium.id), "name": "Premium", "color": "#aa0000", "price": "80.00", "available": True},
            {"id": str(standard.id), "name": "Standard", "color": "#00aa00", "price": "40.00", "available": True},
        ],
        # Seats 5 and 6 are unpainted: they cost the tier's flat price.
        "unpainted": "50.00",
    }


def test_painted_but_unpriced_category_is_still_listed(
    client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    """Paint mutates after tier validation (spec §4.3): the drifted category is listed, unpriced.

    It is listed rather than omitted so the frontend can render those seats greyed out —
    a category that silently vanished would leave its seats unexplained and
    indistinguishable from unpainted ones. It carries **no price**: checkout refuses such
    a seat (decision 2026-07-20), so any number here would be an offer the platform will
    not honour.
    """
    event, seats = seated_event
    venue = event.venue
    assert venue is not None
    premium = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000", display_order=0)
    _paint(seats[:2], premium)
    tier = _tier(event, seats, {str(premium.id): "80.00"})

    # Repaint happens *after* the tier was saved and validated — this is the drift case.
    late = PriceCategory.objects.create(venue=venue, name="Zone C", color="#0000aa", display_order=2)
    _paint(seats[2:3], late)

    pricing = _tier_payload(client, event, tier)["seat_pricing"]
    assert pricing["categories"] == [
        {"id": str(premium.id), "name": "Premium", "color": "#aa0000", "price": "80.00", "available": True},
        {"id": str(late.id), "name": "Zone C", "color": "#0000aa", "price": None, "available": False},
    ]

    # The unpainted quote is a real offer — the resolver charges exactly it.
    tier.refresh_from_db()
    price_map = parse_price_map(tier.category_prices)
    assert resolve_seat_price(tier, seats[4], price_map) == Decimal("50.00")  # unpainted
    # The drifted category is not: buying that seat is refused, naming the category.
    with pytest.raises(HttpError) as exc_info:
        resolve_seat_price(tier, seats[2], price_map)
    assert exc_info.value.status_code == 400
    assert "Zone C" in str(exc_info.value.message)


def test_best_available_tier_quotes_no_unpainted_price(
    client: Client, seated_event: tuple[Event, list[VenueSeat]]
) -> None:
    """A mapped best-available tier can never sell an unpainted seat, so it must not price one.

    A zone is mandatory on a mapped tier and the candidate pool is filtered to that zone's
    category, so an unpainted seat is never a candidate. "Other seats: €50" would quote a
    price no buyer can ever be charged.
    """
    event, seats = seated_event
    venue = event.venue
    assert venue is not None
    premium = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000", display_order=0)
    _paint(seats[:2], premium)
    tier = _tier(event, seats, {str(premium.id): "80.00"}, name="BA")
    tier.seat_assignment_mode = TicketTier.SeatAssignmentMode.BEST_AVAILABLE
    tier.save(update_fields=["seat_assignment_mode"])

    pricing = _tier_payload(client, event, tier)["seat_pricing"]
    assert pricing["unpainted"] is None
    # The zones are exactly the priced categories, all of them buyable.
    assert pricing["categories"] == [
        {"id": str(premium.id), "name": "Premium", "color": "#aa0000", "price": "80.00", "available": True}
    ]


def test_flat_tiers_cost_no_extra_queries(
    client: Client,
    seated_event: tuple[Event, list[VenueSeat]],
    django_assert_num_queries: t.Any,
) -> None:
    """Adding flat tiers must not move the query count — the common path stays free."""
    event, seats = seated_event
    with CaptureQueriesContext(connection) as captured:
        client.get(f"/api/events/{event.id}/tickets/tiers")
    before = len(captured.captured_queries)

    for i in range(3):
        _tier(event, seats, {}, name=f"Flat {i}")

    with django_assert_num_queries(before):
        client.get(f"/api/events/{event.id}/tickets/tiers")


def test_seat_pricing_does_not_scale_with_seat_count(
    client: Client,
    seated_event: tuple[Event, list[VenueSeat]],
    django_assert_num_queries: t.Any,
) -> None:
    """One query per category-priced tier, regardless of how many seats the sector holds."""
    event, seats = seated_event
    venue = event.venue
    assert venue is not None
    premium = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")
    _paint(seats, premium)
    _tier(event, seats, {str(premium.id): "80.00"}, name="Priced")

    with CaptureQueriesContext(connection) as captured:
        client.get(f"/api/events/{event.id}/tickets/tiers")
    one_priced_tier = len(captured.captured_queries)

    # Triple the sector's seats: the resolver aggregates in SQL, so nothing moves.
    sector = seats[0].sector
    for i in range(7, 20):
        VenueSeat.objects.create(
            sector=sector,
            label=f"A{i}",
            row_label="A",
            number=i,
            adjacency_index=i - 1,
            default_price_category=premium,
        )
    with django_assert_num_queries(one_priced_tier):
        client.get(f"/api/events/{event.id}/tickets/tiers")

    # ...and each additional category-priced tier costs exactly one more query.
    for i in range(3):
        _tier(event, seats, {str(premium.id): "80.00"}, name=f"Priced {i}")
    with django_assert_num_queries(one_priced_tier + 3):
        client.get(f"/api/events/{event.id}/tickets/tiers")
