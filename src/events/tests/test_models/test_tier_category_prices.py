"""Tests for ``TicketTier.category_prices`` — the per-seat-category price map.

Covers spec §4.2 (write-time validation) and §4.3 (mandatory coverage of every
painted category). No pricing behaviour is attached to the map yet.
"""

import uuid

import pytest
from django.core.exceptions import ValidationError

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
    return Venue.objects.create(organization=organization, name="Main Hall")


@pytest.fixture
def sector(venue: Venue) -> VenueSector:
    return VenueSector.objects.create(venue=venue, name="Stalls")


@pytest.fixture
def premium(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")


@pytest.fixture
def standard(venue: Venue) -> PriceCategory:
    return PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa")


def paint(sector: VenueSector, label: str, category: PriceCategory | None, *, is_active: bool = True) -> VenueSeat:
    """Materialize one seat in ``sector``, optionally painted with ``category``."""
    return VenueSeat.objects.create(
        sector=sector,
        label=label,
        row_label="A",
        number=int(label[1:]),
        adjacency_index=int(label[1:]) - 1,
        default_price_category=category,
        is_active=is_active,
    )


def make_tier(event: Event, sector: VenueSector, **kwargs: object) -> TicketTier:
    """Build (unsaved) a user-choice tier on ``sector``."""
    defaults: dict[str, object] = {
        "event": event,
        "name": "Stalls",
        "sector": sector,
        "seat_assignment_mode": TicketTier.SeatAssignmentMode.USER_CHOICE,
    }
    defaults.update(kwargs)
    return TicketTier(**defaults)


def test_category_prices_defaults_to_empty_dict(event: Event) -> None:
    """A tier with no map configured stores an empty dict, not None."""
    tier = TicketTier.objects.create(event=event, name="GA")
    tier.refresh_from_db()
    assert tier.category_prices == {}


def test_empty_map_is_valid_in_every_mode(event: Event, venue: Venue, sector: VenueSector) -> None:
    """An empty map imposes no constraints — every existing tier stays legal (flat pricing)."""
    tier = TicketTier(
        event=event,
        name="Best Available",
        venue=venue,
        sector=sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
    )
    tier.full_clean()


def test_empty_map_skips_the_coverage_check(
    event: Event, sector: VenueSector, premium: PriceCategory, standard: PriceCategory
) -> None:
    """A user-choice tier with painted-but-unpriced categories is legal while the map is empty."""
    paint(sector, "A1", premium)
    paint(sector, "A2", standard)
    make_tier(event, sector).full_clean()


def test_full_coverage_user_choice_tier_is_valid(
    event: Event, sector: VenueSector, premium: PriceCategory, standard: PriceCategory
) -> None:
    """Every painted category priced → valid."""
    paint(sector, "A1", premium)
    paint(sector, "A2", standard)
    tier = make_tier(
        event,
        sector,
        category_prices={str(premium.id): "50.00", str(standard.id): "30.00"},
    )
    tier.full_clean()


# --- Rule 1 & 2: seat assignment mode ---


def test_map_rejected_for_unseated_tier(event: Event, venue: Venue, premium: PriceCategory) -> None:
    """seat_assignment_mode=NONE has no seats, so it can have no category prices."""
    tier = TicketTier(event=event, name="GA", venue=venue, category_prices={str(premium.id): "10.00"})
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict


def test_map_accepted_for_best_available_tier(event: Event, sector: VenueSector, premium: PriceCategory) -> None:
    """v3: the map is the single pricing mechanism — best_available uses it too."""
    paint(sector, "A1", premium)
    tier = make_tier(
        event,
        sector,
        name="Best Available",
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        category_prices={str(premium.id): "10.00"},
    )
    tier.full_clean()


def test_partial_map_valid_for_best_available_tier(
    event: Event, sector: VenueSector, premium: PriceCategory, standard: PriceCategory
) -> None:
    """The keys define the tier's sellable zones; an unpriced painted category is simply not one."""
    paint(sector, "A1", premium)
    paint(sector, "A2", standard)
    tier = make_tier(
        event,
        sector,
        name="Premium Only",
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        category_prices={str(premium.id): "50.00"},
    )
    tier.full_clean()


def test_partial_map_rejected_for_user_choice_tier(
    event: Event, sector: VenueSector, premium: PriceCategory, standard: PriceCategory
) -> None:
    """A user-choice buyer can click any seat, so every painted category must be priced."""
    paint(sector, "A1", premium)
    paint(sector, "A2", standard)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00"})
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict


def test_flipping_user_choice_to_best_available_allowed(
    event: Event, sector: VenueSector, premium: PriceCategory
) -> None:
    """Both modes price through the map, so a mode flip needs no map surgery."""
    paint(sector, "A1", premium)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00"})
    tier.save()

    tier.seat_assignment_mode = TicketTier.SeatAssignmentMode.BEST_AVAILABLE
    tier.save()
    tier.refresh_from_db()
    assert tier.seat_assignment_mode == TicketTier.SeatAssignmentMode.BEST_AVAILABLE


def test_flipping_best_available_to_user_choice_needs_full_coverage(
    event: Event, sector: VenueSector, premium: PriceCategory, standard: PriceCategory
) -> None:
    """A partial map is legal for best_available but a hole for user_choice."""
    paint(sector, "A1", premium)
    paint(sector, "A2", standard)
    tier = make_tier(
        event,
        sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
        category_prices={str(premium.id): "50.00"},
    )
    tier.save()

    tier.seat_assignment_mode = TicketTier.SeatAssignmentMode.USER_CHOICE
    with pytest.raises(ValidationError) as exc_info:
        tier.save()
    assert "category_prices" in exc_info.value.message_dict


def test_flipping_mode_to_none_requires_clearing_the_map(
    event: Event, sector: VenueSector, premium: PriceCategory
) -> None:
    """An unseated tier has no seats, so it can carry no category prices."""
    paint(sector, "A1", premium)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00"})
    tier.save()

    tier.seat_assignment_mode = TicketTier.SeatAssignmentMode.NONE
    with pytest.raises(ValidationError) as exc_info:
        tier.save()
    assert "category_prices" in exc_info.value.message_dict

    tier.category_prices = {}
    tier.save()
    tier.refresh_from_db()
    assert tier.seat_assignment_mode == TicketTier.SeatAssignmentMode.NONE


# --- Rule 3: venue scope ---


def test_foreign_venue_category_rejected(
    event: Event, organization: Organization, sector: VenueSector, premium: PriceCategory
) -> None:
    """A category from another venue is not addressable by this tier."""
    other_venue = Venue.objects.create(organization=organization, name="Annex")
    foreign = PriceCategory.objects.create(venue=other_venue, name="Annex Balcony", color="#aa0000")
    paint(sector, "A1", premium)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00", str(foreign.id): "20.00"})
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict
    # Named, not a bare UUID — the tier form has to render this to an admin.
    assert "Annex Balcony" in exc_info.value.message_dict["category_prices"][0]
    assert str(foreign.id) not in exc_info.value.message_dict["category_prices"][0]


def test_unknown_category_id_rejected(event: Event, sector: VenueSector) -> None:
    """A key that resolves to no category at all is a validation error."""
    tier = make_tier(event, sector, category_prices={str(uuid.uuid4()): "50.00"})
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict


# --- Rule 4: PWYC exclusivity, both directions ---


def test_pwyc_tier_cannot_have_category_prices(event: Event, sector: VenueSector, premium: PriceCategory) -> None:
    """Adding prices to a PWYC tier is rejected."""
    paint(sector, "A1", premium)
    tier = make_tier(
        event,
        sector,
        price_type=TicketTier.PriceType.PWYC,
        category_prices={str(premium.id): "50.00"},
    )
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict


def test_flipping_priced_tier_to_pwyc_rejected(event: Event, sector: VenueSector, premium: PriceCategory) -> None:
    """Flipping an already-priced tier to PWYC is rejected too."""
    paint(sector, "A1", premium)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00"})
    tier.save()

    tier.price_type = TicketTier.PriceType.PWYC
    with pytest.raises(ValidationError) as exc_info:
        tier.save()
    assert "category_prices" in exc_info.value.message_dict


# --- Rule 5: ONLINE minimum ---


def test_online_tier_rejects_category_price_below_one(
    event: Event, sector: VenueSector, premium: PriceCategory
) -> None:
    """The ≥1 floor that applies to the flat price applies to every mapped price."""
    paint(sector, "A1", premium)
    tier = make_tier(
        event,
        sector,
        payment_method=TicketTier.PaymentMethod.ONLINE,
        price="10.00",
        category_prices={str(premium.id): "0.50"},
    )
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict


def test_offline_tier_allows_category_price_below_one(
    event: Event, sector: VenueSector, premium: PriceCategory
) -> None:
    """The floor is ONLINE-only — a door tier may sell a category for 0.50."""
    paint(sector, "A1", premium)
    tier = make_tier(
        event,
        sector,
        payment_method=TicketTier.PaymentMethod.AT_THE_DOOR,
        category_prices={str(premium.id): "0.50"},
    )
    tier.full_clean()


# --- Rule 6: value parsing ---


@pytest.mark.parametrize("value", ["abc", "", None, [], "1,50"])
def test_malformed_price_value_rejected(
    event: Event, sector: VenueSector, premium: PriceCategory, value: object
) -> None:
    """Garbage values are a validation error, never a 500."""
    paint(sector, "A1", premium)
    tier = make_tier(event, sector, category_prices={str(premium.id): value})
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict


def test_negative_price_rejected(event: Event, sector: VenueSector, premium: PriceCategory) -> None:
    """Negative money is never valid."""
    paint(sector, "A1", premium)
    tier = make_tier(event, sector, category_prices={str(premium.id): "-1.00"})
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict


def test_non_uuid_key_rejected(event: Event, sector: VenueSector, premium: PriceCategory) -> None:
    """Keys must be UUID strings."""
    paint(sector, "A1", premium)
    tier = make_tier(event, sector, category_prices={"premium": "50.00"})
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict


def test_non_mapping_value_rejected(event: Event, sector: VenueSector) -> None:
    """The field must hold a mapping."""
    tier = make_tier(event, sector, category_prices=["not", "a", "map"])
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    assert "category_prices" in exc_info.value.message_dict


# --- Rule 7: full coverage (spec §4.3) ---


def test_missing_painted_category_rejected_and_named(
    event: Event, sector: VenueSector, premium: PriceCategory, standard: PriceCategory
) -> None:
    """Every category painted on an active seat in the sector must be priced, by name."""
    paint(sector, "A1", premium)
    paint(sector, "A2", standard)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00"})
    with pytest.raises(ValidationError) as exc_info:
        tier.full_clean()
    message = exc_info.value.message_dict["category_prices"][0]
    assert "Standard" in message
    assert "Premium" not in message


def test_unpainted_seats_do_not_require_coverage(event: Event, sector: VenueSector, premium: PriceCategory) -> None:
    """Seats with no category fall back to the flat price at runtime — legal."""
    paint(sector, "A1", premium)
    paint(sector, "A2", None)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00"})
    tier.full_clean()


def test_inactive_seats_do_not_require_coverage(
    event: Event, sector: VenueSector, premium: PriceCategory, standard: PriceCategory
) -> None:
    """Deactivated seats are not sellable, so their category needs no price."""
    paint(sector, "A1", premium)
    paint(sector, "A2", standard, is_active=False)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00"})
    tier.full_clean()


def test_categories_painted_in_other_sectors_do_not_require_coverage(
    event: Event, venue: Venue, sector: VenueSector, premium: PriceCategory, standard: PriceCategory
) -> None:
    """Coverage is scoped to the tier's own sector."""
    other_sector = VenueSector.objects.create(venue=venue, name="Balcony")
    paint(sector, "A1", premium)
    paint(other_sector, "A1", standard)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00"})
    tier.full_clean()


def test_pricing_an_unpainted_but_valid_category_is_allowed(
    event: Event, sector: VenueSector, premium: PriceCategory, standard: PriceCategory
) -> None:
    """Over-coverage is harmless: pricing a category nobody painted yet is fine."""
    paint(sector, "A1", premium)
    tier = make_tier(event, sector, category_prices={str(premium.id): "50.00", str(standard.id): "30.00"})
    tier.full_clean()
