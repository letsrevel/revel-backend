"""Admin API round-trips for ``TicketTier.category_prices`` (spec §7).

Covers the write contract (null/omitted = untouched, ``{}`` = clear, non-empty =
replace), the read contract (``TicketTierDetailSchema`` re-hydrates the map), and
that the write-time validation rules from ``events.utils.tier_pricing`` surface as
clean 400s rather than 500s or silent coercions.
"""

import typing as t
import uuid

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import (
    Event,
    Organization,
    PriceCategory,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def venue(organization: Organization) -> Venue:
    """A venue owned by the event's organization."""
    return Venue.objects.create(organization=organization, name="Main Hall")


@pytest.fixture
def premium(venue: Venue) -> PriceCategory:
    """The expensive category, painted on seat A1."""
    return PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")


@pytest.fixture
def standard(venue: Venue) -> PriceCategory:
    """The cheap category, painted on seat A2."""
    return PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa")


@pytest.fixture
def sector(venue: Venue, premium: PriceCategory, standard: PriceCategory) -> VenueSector:
    """A sector with one Premium seat, one Standard seat, and one unpainted seat."""
    sector = VenueSector.objects.create(venue=venue, name="Stalls")
    for number, category in ((1, premium), (2, standard), (3, None)):
        VenueSeat.objects.create(
            sector=sector,
            label=f"A{number}",
            row_label="A",
            number=number,
            adjacency_index=number - 1,
            default_price_category=category,
        )
    return sector


def create_payload(sector: VenueSector, **overrides: t.Any) -> dict[str, t.Any]:
    """Build a valid user-choice tier create payload."""
    payload: dict[str, t.Any] = {
        "name": "Stalls",
        "price": "30.00",
        "payment_method": "offline",
        "seat_assignment_mode": "user_choice",
        "sector_id": str(sector.pk),
    }
    payload.update(overrides)
    return payload


def post_tier(client: Client, event: Event, payload: dict[str, t.Any]) -> t.Any:
    """POST the tier create endpoint."""
    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    return client.post(url, data=orjson.dumps(payload), content_type="application/json")


def put_tier(client: Client, event: Event, tier: TicketTier, payload: dict[str, t.Any]) -> t.Any:
    """PUT the tier update endpoint."""
    url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": tier.pk})
    return client.put(url, data=orjson.dumps(payload), content_type="application/json")


@pytest.fixture
def priced_tier(
    organization_owner_client: Client,
    event: Event,
    sector: VenueSector,
    premium: PriceCategory,
    standard: PriceCategory,
) -> TicketTier:
    """A persisted, fully-covered category-priced tier created through the API."""
    response = post_tier(
        organization_owner_client,
        event,
        create_payload(sector, category_prices={str(premium.pk): "50.00", str(standard.pk): "30.00"}),
    )
    assert response.status_code == 200, response.json()
    return TicketTier.objects.get(pk=response.json()["id"])


# ---- Create ----


def test_create_with_category_prices_round_trips(
    organization_owner_client: Client,
    event: Event,
    sector: VenueSector,
    premium: PriceCategory,
    standard: PriceCategory,
) -> None:
    """A non-empty map is stored verbatim and echoed back by the detail schema."""
    expected = {str(premium.pk): "50.00", str(standard.pk): "30.00"}
    response = post_tier(organization_owner_client, event, create_payload(sector, category_prices=expected))

    assert response.status_code == 200, response.json()
    assert response.json()["category_prices"] == expected
    assert TicketTier.objects.get(pk=response.json()["id"]).category_prices == expected


def test_create_without_category_prices_defaults_to_empty_map(
    organization_owner_client: Client, event: Event, sector: VenueSector
) -> None:
    """Omitting the field leaves a flat-priced tier — the map defaults to empty, not null."""
    response = post_tier(organization_owner_client, event, create_payload(sector))

    assert response.status_code == 200, response.json()
    assert response.json()["category_prices"] == {}
    assert TicketTier.objects.get(pk=response.json()["id"]).category_prices == {}


def test_create_with_explicit_null_category_prices_defaults_to_empty_map(
    organization_owner_client: Client, event: Event, sector: VenueSector
) -> None:
    """An explicit null must not reach the NOT NULL column — it means "leave at default"."""
    response = post_tier(organization_owner_client, event, create_payload(sector, category_prices=None))

    assert response.status_code == 200, response.json()
    assert response.json()["category_prices"] == {}
    assert TicketTier.objects.get(pk=response.json()["id"]).category_prices == {}


# ---- Update: the three-way contract ----


def test_update_omitting_category_prices_leaves_the_map_intact(
    organization_owner_client: Client, event: Event, priced_tier: TicketTier
) -> None:
    """An unrelated rename must not clear prices — omitted means untouched."""
    before = priced_tier.category_prices

    response = put_tier(organization_owner_client, event, priced_tier, {"name": "Renamed"})

    assert response.status_code == 200, response.json()
    assert response.json()["name"] == "Renamed"
    assert response.json()["category_prices"] == before
    priced_tier.refresh_from_db()
    assert priced_tier.category_prices == before


def test_update_with_null_category_prices_leaves_the_map_intact(
    organization_owner_client: Client, event: Event, priced_tier: TicketTier
) -> None:
    """Explicit null is treated exactly like omitted."""
    before = priced_tier.category_prices

    response = put_tier(organization_owner_client, event, priced_tier, {"category_prices": None})

    assert response.status_code == 200, response.json()
    assert response.json()["category_prices"] == before
    priced_tier.refresh_from_db()
    assert priced_tier.category_prices == before


def test_update_with_empty_map_clears_the_prices(
    organization_owner_client: Client, event: Event, priced_tier: TicketTier
) -> None:
    """An empty object is the explicit "clear" signal."""
    response = put_tier(organization_owner_client, event, priced_tier, {"category_prices": {}})

    assert response.status_code == 200, response.json()
    assert response.json()["category_prices"] == {}
    priced_tier.refresh_from_db()
    assert priced_tier.category_prices == {}


def test_update_with_non_empty_map_replaces_wholesale(
    organization_owner_client: Client,
    event: Event,
    priced_tier: TicketTier,
    premium: PriceCategory,
    standard: PriceCategory,
) -> None:
    """A non-empty map replaces the stored one — it is not merged into it."""
    replacement = {str(premium.pk): "75.00", str(standard.pk): "45.00"}

    response = put_tier(organization_owner_client, event, priced_tier, {"category_prices": replacement})

    assert response.status_code == 200, response.json()
    assert response.json()["category_prices"] == replacement
    priced_tier.refresh_from_db()
    assert priced_tier.category_prices == replacement


# ---- Read ----


def test_list_ticket_tiers_exposes_the_map(
    organization_owner_client: Client, event: Event, priced_tier: TicketTier
) -> None:
    """The list endpoint carries the map too, so the admin table can render prices."""
    url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    tiers = {r["id"]: r for r in response.json()["results"]}
    assert tiers[str(priced_tier.pk)]["category_prices"] == priced_tier.category_prices


def test_list_ticket_tiers_reports_no_gaps_while_the_map_is_complete(
    organization_owner_client: Client, event: Event, priced_tier: TicketTier
) -> None:
    """A fully-covered tier must not cry wolf — the form only warns on a real gap."""
    url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    tiers = {r["id"]: r for r in response.json()["results"]}
    assert tiers[str(priced_tier.pk)]["pricing_gaps"] == []


def test_a_late_repaint_surfaces_as_a_pricing_gap(
    organization_owner_client: Client,
    event: Event,
    venue: Venue,
    sector: VenueSector,
    priced_tier: TicketTier,
) -> None:
    """Painting a new category after the tier was saved leaves a gap only the admin can fix.

    ``paint_seats`` is venue-scoped and never fails (spec §4.3), so the tier silently
    stops covering its sector and checkout starts refusing those seats. This payload is
    the admin's only warning.
    """
    balcony = PriceCategory.objects.create(venue=venue, name="Balcony", color="#00aa00")
    seat = sector.seats.get(label="A3")  # the unpainted one
    seat.default_price_category = balcony
    seat.save(update_fields=["default_price_category"])

    url = reverse("api:list_ticket_tiers", kwargs={"event_id": event.pk})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    tiers = {r["id"]: r for r in response.json()["results"]}
    assert tiers[str(priced_tier.pk)]["pricing_gaps"] == [
        {"id": str(balcony.pk), "name": "Balcony", "color": "#00aa00"}
    ]


# ---- Validation: clean 400s, never 500s, never silent coercion ----


def test_json_float_price_is_rejected_with_400(
    organization_owner_client: Client,
    event: Event,
    sector: VenueSector,
    premium: PriceCategory,
    standard: PriceCategory,
) -> None:
    """A JSON number like 50.0 is refused outright — binary floats cannot represent money."""
    response = post_tier(
        organization_owner_client,
        event,
        create_payload(sector, category_prices={str(premium.pk): 50.0, str(standard.pk): "30.00"}),
    )

    assert response.status_code == 400, response.json()
    assert "decimal string" in response.json()["errors"]["category_prices"][0]
    assert not TicketTier.objects.filter(name="Stalls").exists()


def test_boolean_price_is_rejected_with_400(
    organization_owner_client: Client,
    event: Event,
    sector: VenueSector,
    premium: PriceCategory,
    standard: PriceCategory,
) -> None:
    """``True`` must not be coerced into a price of 1."""
    response = post_tier(
        organization_owner_client,
        event,
        create_payload(sector, category_prices={str(premium.pk): True, str(standard.pk): "30.00"}),
    )

    assert response.status_code == 400, response.json()
    assert "decimal string" in response.json()["errors"]["category_prices"][0]


def test_integer_price_is_accepted(
    organization_owner_client: Client,
    event: Event,
    sector: VenueSector,
    premium: PriceCategory,
    standard: PriceCategory,
) -> None:
    """Integers are exact, so they are legal (and stored as sent)."""
    payload_map = {str(premium.pk): 50, str(standard.pk): "30.00"}
    response = post_tier(organization_owner_client, event, create_payload(sector, category_prices=payload_map))

    assert response.status_code == 200, response.json()
    assert TicketTier.objects.get(pk=response.json()["id"]).category_prices == payload_map


def test_missing_painted_category_is_rejected_naming_it(
    organization_owner_client: Client, event: Event, sector: VenueSector, premium: PriceCategory
) -> None:
    """The §4.3 coverage rule surfaces as a 400 naming the unpriced categories."""
    response = post_tier(
        organization_owner_client, event, create_payload(sector, category_prices={str(premium.pk): "50.00"})
    )

    assert response.status_code == 400, response.json()
    message = response.json()["errors"]["category_prices"][0]
    assert "Standard" in message
    assert "must be priced" in message


def test_unknown_category_is_rejected_with_400(
    organization_owner_client: Client,
    event: Event,
    sector: VenueSector,
    premium: PriceCategory,
    standard: PriceCategory,
) -> None:
    """A category id from another venue (or nowhere at all) is a 400, not a 500."""
    stranger = uuid.uuid4()
    response = post_tier(
        organization_owner_client,
        event,
        create_payload(
            sector,
            category_prices={str(premium.pk): "50.00", str(standard.pk): "30.00", str(stranger): "10.00"},
        ),
    )

    assert response.status_code == 400, response.json()
    assert str(stranger) in response.json()["errors"]["category_prices"][0]


def test_malformed_category_key_is_rejected_with_400(
    organization_owner_client: Client, event: Event, sector: VenueSector
) -> None:
    """A non-UUID key is a 400, not an unhandled ValueError."""
    response = post_tier(organization_owner_client, event, create_payload(sector, category_prices={"premium": "50.00"}))

    assert response.status_code == 400, response.json()
    assert "not a valid price category id" in response.json()["errors"]["category_prices"][0]


def test_unseated_tier_cannot_be_category_priced(
    organization_owner_client: Client, event: Event, sector: VenueSector, premium: PriceCategory
) -> None:
    """Category pricing needs seats: seat_assignment_mode=none has none (spec §4.2 rule 1)."""
    response = post_tier(
        organization_owner_client,
        event,
        {
            "name": "General",
            "price": "30.00",
            "payment_method": "offline",
            "category_prices": {str(premium.pk): "50.00"},
        },
    )

    assert response.status_code == 400, response.json()
    assert "seated tier" in response.json()["errors"]["category_prices"][0]


def test_best_available_tier_can_be_category_priced(
    organization_owner_client: Client, event: Event, sector: VenueSector, premium: PriceCategory
) -> None:
    """v3: the map is the single pricing mechanism, so best-available uses it too."""
    response = post_tier(
        organization_owner_client,
        event,
        {
            "name": "Premium Zone",
            "price": "30.00",
            "payment_method": "offline",
            "seat_assignment_mode": "best_available",
            "sector_id": str(sector.pk),
            "category_prices": {str(premium.pk): "50.00"},
        },
    )

    assert response.status_code == 200, response.json()
    tier = TicketTier.objects.get(pk=response.json()["id"])
    assert tier.category_prices == {str(premium.pk): "50.00"}
    # Partial coverage: Standard is painted in the sector but is simply not a zone of this tier.
    assert response.json()["pricing_gaps"] == []


def test_switching_a_priced_tier_to_pwyc_is_rejected(
    organization_owner_client: Client, event: Event, priced_tier: TicketTier
) -> None:
    """PWYC and category pricing are mutually exclusive, in both directions."""
    response = put_tier(organization_owner_client, event, priced_tier, {"price_type": "pwyc"})

    assert response.status_code == 400, response.json()
    assert "pay-what-you-can" in response.json()["errors"]["category_prices"][0]
    priced_tier.refresh_from_db()
    assert priced_tier.price_type == TicketTier.PriceType.FIXED
