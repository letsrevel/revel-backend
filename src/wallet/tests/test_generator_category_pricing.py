"""The Wallet pass price agrees with the refund ceiling on a category-priced tier (#754).

``ApplePassGenerator._resolve_price`` was the fourth ``price_paid → payment.amount →
tier.price`` chain and the only one #739 did not revisit. Its last leg now goes through the
same ``recorded_or_resolved_price`` the refund ceiling (``ticket_service``) and the revenue
report (``revenue_aggregation``) use, so a ticket sold before its tier opted into category
pricing cannot print one number on the attendee's phone and refund a different one.
"""

import typing as t
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from accounts.models import RevelUser
from events.models import Event, PriceCategory, Ticket, TicketTier, Venue, VenueSeat, VenueSector
from events.models.ticket import Payment
from wallet.apple.generator import ApplePassGenerator

pytestmark = pytest.mark.django_db

PREMIUM = Decimal("80.00")
FLAT = Decimal("30.00")


@pytest.fixture
def venue(organization: t.Any) -> Venue:
    """A venue for the seated tier."""
    return Venue.objects.create(organization=organization, name="Teatro Grande", capacity=100)


@pytest.fixture
def seated_event(event: Event, venue: Venue) -> Event:
    """The wallet test event, held at the venue."""
    event.venue = venue
    event.save(update_fields=["venue"])
    return event


@pytest.fixture
def sector(venue: Venue) -> VenueSector:
    """The sector the tier sells."""
    return VenueSector.objects.create(venue=venue, name="Platea")


@pytest.fixture
def premium(venue: Venue) -> PriceCategory:
    """The category priced above the tier's flat price."""
    return PriceCategory.objects.create(venue=venue, name="Platea Premium", color="#aa0000", display_order=0)


@pytest.fixture
def premium_seat(sector: VenueSector, premium: PriceCategory) -> VenueSeat:
    """A seat painted into the premium category."""
    return VenueSeat.objects.create(sector=sector, label="A-7", row_label="A", number=7, default_price_category=premium)


@pytest.fixture
def unpainted_seat(sector: VenueSector) -> VenueSeat:
    """A seat with no price category — it charges the tier's flat price."""
    return VenueSeat.objects.create(sector=sector, label="Z-1", row_label="Z", number=1)


@pytest.fixture
def category_priced_tier(
    seated_event: Event,
    venue: Venue,
    sector: VenueSector,
    premium: PriceCategory,
    premium_seat: VenueSeat,
) -> TicketTier:
    """A flat €30 user-choice tier that has since priced Platea Premium at €80."""
    return TicketTier.objects.create(
        event=seated_event,
        name="Platea",
        price=FLAT,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        venue=venue,
        sector=sector,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        category_prices={str(premium.id): str(PREMIUM)},
    )


@pytest.fixture
def legacy_ticket(
    seated_event: Event,
    member_user: RevelUser,
    category_priced_tier: TicketTier,
    premium_seat: VenueSeat,
) -> Ticket:
    """A box-office ticket for A-7 issued while the tier was still flat.

    ``price_paid`` is NULL by design for a flat tier (``box_office.py``), and there is no
    Stripe payment row — the exact shape the fourth chain got wrong.
    """
    return Ticket.objects.create(
        event=seated_event,
        user=member_user,
        tier=category_priced_tier,
        seat=premium_seat,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=member_user.get_display_name(),
    )


def _price(ticket: Ticket, signer: MagicMock) -> str:
    """The price string the pass would print for this ticket."""
    return ApplePassGenerator(signer=signer)._build_pass_data(ticket).ticket_price


class TestCategoryPricedPassPrice:
    """What the pass prints when ``price_paid`` is NULL on a category-priced tier."""

    def test_resolves_the_seats_category_price(self, legacy_ticket: Ticket, mock_signer: MagicMock) -> None:
        """The pass prints €80 — what a refund of this ticket would pay out — not the flat €30."""
        assert legacy_ticket.price_paid is None
        assert _price(legacy_ticket, mock_signer) == "EUR 80.00"

    def test_agrees_with_the_refund_ceiling(self, legacy_ticket: Ticket, mock_signer: MagicMock) -> None:
        """The whole point: the printed number and the money-bearing helper cannot diverge."""
        from events.service.seating.pricing import recorded_or_resolved_price

        expected = recorded_or_resolved_price(legacy_ticket.tier, legacy_ticket.seat, legacy_ticket.price_paid)

        assert _price(legacy_ticket, mock_signer) == f"EUR {expected:.2f}"

    def test_recorded_price_paid_still_wins(self, legacy_ticket: Ticket, mock_signer: MagicMock) -> None:
        """Purchase-time truth outranks any re-resolution — an at-the-door discount stays €12."""
        legacy_ticket.price_paid = Decimal("12.00")
        legacy_ticket.save()

        assert _price(legacy_ticket, mock_signer) == "EUR 12.00"

    def test_payment_amount_still_wins_over_the_category(
        self, legacy_ticket: Ticket, member_user: RevelUser, mock_signer: MagicMock
    ) -> None:
        """An online ticket carries NULL ``price_paid``; its payment row is the answer, not the seat."""
        Payment.objects.create(
            ticket=legacy_ticket,
            user=member_user,
            stripe_session_id="cs_test_754",
            status=Payment.PaymentStatus.SUCCEEDED,
            amount=Decimal("55.00"),
            platform_fee=Decimal("2.75"),
            currency="EUR",
        )
        ticket = Ticket.objects.full().get(pk=legacy_ticket.pk)

        assert _price(ticket, mock_signer) == "EUR 55.00"

    def test_unpainted_seat_falls_back_to_the_flat_price(
        self,
        seated_event: Event,
        member_user: RevelUser,
        category_priced_tier: TicketTier,
        unpainted_seat: VenueSeat,
        mock_signer: MagicMock,
    ) -> None:
        """A seat in no category is not repriced — the tier's flat price still applies."""
        ticket = Ticket.objects.create(
            event=seated_event,
            user=member_user,
            tier=category_priced_tier,
            seat=unpainted_seat,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=member_user.get_display_name(),
        )

        assert _price(ticket, mock_signer) == "EUR 30.00"

    def test_general_admission_ticket_falls_back_to_the_flat_price(
        self,
        seated_event: Event,
        member_user: RevelUser,
        category_priced_tier: TicketTier,
        mock_signer: MagicMock,
    ) -> None:
        """No seat at all — nothing to resolve from, so the flat price stands."""
        ticket = Ticket.objects.create(
            event=seated_event,
            user=member_user,
            tier=category_priced_tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=member_user.get_display_name(),
        )

        assert _price(ticket, mock_signer) == "EUR 30.00"

    def test_resolution_costs_no_extra_query(
        self, legacy_ticket: Ticket, mock_signer: MagicMock, django_assert_num_queries: t.Any
    ) -> None:
        """Passes are generated one per ticket, sometimes per email — this must not add a query.

        ``Ticket.objects.full()`` (used by both the download endpoint and the notification
        attachment builder) already selects ``tier`` and ``seat``, and the resolver reads only
        ``tier.category_prices`` and ``seat.default_price_category_id``, both plain columns on
        rows that are already in memory.
        """
        ticket = Ticket.objects.full().get(pk=legacy_ticket.pk)

        with django_assert_num_queries(0):
            price, currency = ApplePassGenerator(signer=mock_signer)._resolve_price(ticket)

        assert (price, currency) == (PREMIUM, "EUR")
