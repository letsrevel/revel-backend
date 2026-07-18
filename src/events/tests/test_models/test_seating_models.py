"""Tests for Phase-1 seating models (PriceCategory, sector kind, seat columns)."""

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeatOverride,
    Organization,
    PriceCategory,
    SeatHold,
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


def test_price_category_creation_and_uniqueness(venue: Venue) -> None:
    cat = PriceCategory.objects.create(venue=venue, name="Premium", color="#aa0000")
    assert cat.display_order == 0
    # TimeStampedModel.save() runs full_clean(), so the (venue, name) uniqueness
    # constraint surfaces as a ValidationError before the DB-level IntegrityError.
    with pytest.raises(ValidationError):
        PriceCategory.objects.create(venue=venue, name="Premium", color="#bb0000")


def test_sector_kind_defaults_to_seated(sector: VenueSector) -> None:
    assert sector.kind == VenueSector.Kind.SEATED


def test_seat_row_and_adjacency_columns(sector: VenueSector, venue: Venue) -> None:
    cat = PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa")
    seat = VenueSeat.objects.create(
        sector=sector,
        label="A1",
        row_label="A",
        row_order=0,
        number=1,
        adjacency_index=0,
        default_price_category=cat,
    )
    assert seat.row_label == "A"
    assert seat.adjacency_index == 0
    assert seat.default_price_category == cat


def test_seat_label_composition_unchanged(sector: VenueSector) -> None:
    """Spec §6.4: composed labels must not change through the migration.

    `label` is an independent column; renaming row→row_label must not affect it.
    """
    seat = VenueSeat.objects.create(sector=sector, label="C-12", row_label="C", number=12)
    assert seat.label == "C-12"
    assert str(seat) == f"{sector.name} / C-12"


def test_tier_accepts_price_category_without_sector(event: Event, venue: Venue) -> None:
    event.venue = venue
    event.save(update_fields=["venue"])
    cat = PriceCategory.objects.create(venue=venue, name="Gold", color="#ffaa00")
    tier = TicketTier.objects.create(
        event=event,
        name="Gold",
        price_category=cat,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
    )
    assert tier.venue_id == venue.id  # auto-filled from category's venue


def test_tier_price_category_wrong_venue_rejected(event: Event, venue: Venue, organization: Organization) -> None:
    other = Venue.objects.create(organization=organization, name="Other Hall")
    cat = PriceCategory.objects.create(venue=other, name="Gold", color="#ffaa00")
    with pytest.raises(DjangoValidationError):
        # Tier pinned to `venue`, but the category lives on `other` → venue mismatch.
        TicketTier.objects.create(event=event, name="Gold", venue=venue, price_category=cat)


def test_seated_mode_requires_sector_or_category(event: Event) -> None:
    with pytest.raises(DjangoValidationError):
        TicketTier.objects.create(
            event=event, name="Bad", seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE
        )


def test_best_available_with_only_sector_rejected(event: Event, sector: VenueSector) -> None:
    """BEST_AVAILABLE picks from the price category's pool — a sector alone is unsellable."""
    with pytest.raises(DjangoValidationError):
        TicketTier.objects.create(
            event=event, name="Bad", sector=sector, seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE
        )


def test_user_choice_with_only_price_category_rejected(event: Event, venue: Venue) -> None:
    """USER_CHOICE assigns within a sector — a price category alone is unsellable."""
    cat = PriceCategory.objects.create(venue=venue, name="Gold", color="#ffaa00")
    with pytest.raises(DjangoValidationError):
        TicketTier.objects.create(
            event=event, name="Bad", price_category=cat, seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE
        )


def test_user_choice_on_standing_sector_rejected(event: Event, venue: Venue) -> None:
    """A standing sector has no seats to choose — every USER_CHOICE hold would 409."""
    event.venue = venue
    event.save(update_fields=["venue"])
    standing = VenueSector.objects.create(venue=venue, name="Pit", kind=VenueSector.Kind.STANDING)
    with pytest.raises(DjangoValidationError, match="seated sector"):
        TicketTier.objects.create(
            event=event, name="Bad", sector=standing, seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE
        )


def test_none_mode_on_standing_sector_still_valid(event: Event, venue: Venue) -> None:
    """GA tiers may point at a standing sector (that is their whole purpose)."""
    event.venue = venue
    event.save(update_fields=["venue"])
    standing = VenueSector.objects.create(venue=venue, name="Pit", kind=VenueSector.Kind.STANDING)
    tier = TicketTier.objects.create(
        event=event, name="GA", sector=standing, seat_assignment_mode=TicketTier.SeatAssignmentMode.NONE
    )
    assert tier.sector_id == standing.id


def test_user_choice_with_sector_still_valid(event: Event, venue: Venue, sector: VenueSector) -> None:
    event.venue = venue
    event.save(update_fields=["venue"])
    tier = TicketTier.objects.create(
        event=event, name="OK", sector=sector, seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE
    )
    assert tier.sector_id == sector.id


def test_two_tiers_may_share_one_category(event: Event, venue: Venue) -> None:
    """Spec §1: concession pricing — adult/student on the same seat pool."""
    event.venue = venue
    event.save(update_fields=["venue"])
    cat = PriceCategory.objects.create(venue=venue, name="Stalls", color="#00aaff")
    a = TicketTier.objects.create(event=event, name="Adult", price_category=cat)
    b = TicketTier.objects.create(event=event, name="Student", price_category=cat)
    assert a.price_category_id == b.price_category_id


@pytest.fixture
def revel_user(django_user_model: type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="seat_holder", email="seat_holder@example.com", password="pw")


# --- EventSeatOverride ------------------------------------------------------


def test_override_unique_per_event_seat(event: Event, sector: VenueSector) -> None:
    """(event, seat) is unique. Proven at the DB level via bulk_create (bypasses full_clean)."""
    seat = VenueSeat.objects.create(sector=sector, label="B1", row_label="B", number=1)
    EventSeatOverride.objects.create(
        event=event, seat=seat, status=EventSeatOverride.OverrideStatus.HELD, reason="house"
    )
    # bulk_create skips TimeStampedModel.save/full_clean, so the duplicate hits the
    # DB UniqueConstraint directly and raises IntegrityError (not ValidationError).
    with pytest.raises(IntegrityError):
        EventSeatOverride.objects.bulk_create(
            [EventSeatOverride(event=event, seat=seat, status=EventSeatOverride.OverrideStatus.KILLED)]
        )


# --- SeatHold ---------------------------------------------------------------


def test_seathold_exactly_one_owner_check(event: Event, sector: VenueSector) -> None:
    """The exactly-one-owner CHECK fires at the DB level when neither owner is set.

    bulk_create bypasses full_clean so this proves the raw-SQL path (which also
    bypasses full_clean) is guarded by a real DB CheckConstraint.
    """
    seat = VenueSeat.objects.create(sector=sector, label="B2", row_label="B", number=2)
    now = timezone.now()
    with pytest.raises(IntegrityError):
        SeatHold.objects.bulk_create(
            [SeatHold(event=event, seat=seat, acquired_at=now, expires_at=now + timedelta(minutes=10))]
        )


def test_seathold_both_owners_violate_check(event: Event, sector: VenueSector, revel_user: RevelUser) -> None:
    """Setting BOTH user and guest_session also violates the exactly-one-owner CHECK."""
    seat = VenueSeat.objects.create(sector=sector, label="B2b", row_label="B", number=22)
    now = timezone.now()
    with pytest.raises(IntegrityError):
        SeatHold.objects.bulk_create(
            [
                SeatHold(
                    event=event,
                    seat=seat,
                    user=revel_user,
                    guest_session="gs-both",
                    acquired_at=now,
                    expires_at=now + timedelta(minutes=10),
                )
            ]
        )


def test_seathold_unconditional_unique(event: Event, sector: VenueSector, revel_user: RevelUser) -> None:
    """(event, seat) is unconditionally unique across ALL rows (no time predicate)."""
    seat = VenueSeat.objects.create(sector=sector, label="B3", row_label="B", number=3)
    now = timezone.now()
    SeatHold.objects.create(
        event=event, seat=seat, user=revel_user, acquired_at=now, expires_at=now + timedelta(minutes=10)
    )
    with pytest.raises(IntegrityError):
        SeatHold.objects.bulk_create(
            [
                SeatHold(
                    event=event,
                    seat=seat,
                    guest_session="gs-x",
                    acquired_at=now,
                    expires_at=now + timedelta(minutes=10),
                )
            ]
        )


def test_seathold_active_manager(event: Event, sector: VenueSector, revel_user: RevelUser) -> None:
    """active() excludes expired holds."""
    seat = VenueSeat.objects.create(sector=sector, label="B4", row_label="B", number=4)
    now = timezone.now()
    SeatHold.objects.create(
        event=event, seat=seat, user=revel_user, acquired_at=now, expires_at=now - timedelta(seconds=1)
    )
    assert SeatHold.objects.active().count() == 0

    seat2 = VenueSeat.objects.create(sector=sector, label="B5", row_label="B", number=5)
    SeatHold.objects.create(
        event=event, seat=seat2, guest_session="gs-live", acquired_at=now, expires_at=now + timedelta(minutes=10)
    )
    assert SeatHold.objects.active().count() == 1


def test_seathold_owner_q(revel_user: RevelUser) -> None:
    """owner_q maps an authenticated user to Q(user=...) and a guest to Q(guest_session=...)."""
    assert SeatHold.owner_q(revel_user, None) == Q(user=revel_user)
    assert SeatHold.owner_q(None, "gs-1") == Q(guest_session="gs-1")
    # An unauthenticated/None identity with no guest session must never match a real guest session.
    assert SeatHold.owner_q(None, None) == Q(guest_session="__none__")
