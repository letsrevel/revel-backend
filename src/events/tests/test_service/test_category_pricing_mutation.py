"""Ticket *mutation* paths and the money-bearing ``price_paid`` fallbacks (plan Task 9).

The spec's ``price_paid`` invariant (§5.5) is time-scoped and, before this task,
violable: ``unconfirm_ticket_payment`` nulled ``price_paid`` unconditionally while
``confirm_ticket_payment`` refuses to restore it for non-PWYC tiers, so a routine
admin unconfirm → confirm cycle produced an ACTIVE, category-priced ticket with no
recorded price. From there both money-bearing fallbacks silently substituted the
tier's flat price.

What is pinned here:

- unconfirm → confirm round-trips a category-priced offline ticket **losslessly**;
- unconfirm still clears an admin-entered PWYC amount (the behaviour it was written for);
- the refund ceiling and the revenue report resolve the *seat's* category price when
  ``price_paid`` is legitimately NULL on a category-priced tier, instead of the flat price;
- neither ever raises — tickets sold before the tier opted into category pricing
  carry NULL by design;
- the revenue report does not go N+1 to do it.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    Organization,
    PriceCategory,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
)
from events.service import ticket_service
from events.service.revenue_aggregation import ReportScope, build_revenue_report_data

pytestmark = pytest.mark.django_db

PREMIUM = Decimal("80.00")
STANDARD = Decimal("30.00")
FLAT = Decimal("50.00")

PRICING_LOGGER = "events.service.seating.pricing"


@pytest.fixture
def sector(organization: Organization) -> VenueSector:
    venue = Venue.objects.create(organization=organization, name="Theatre", capacity=100)
    return VenueSector.objects.create(venue=venue, name="Stalls")


@pytest.fixture
def categories(sector: VenueSector) -> tuple[PriceCategory, PriceCategory]:
    premium = PriceCategory.objects.create(venue=sector.venue, name="Premium", color="#aa0000")
    standard = PriceCategory.objects.create(venue=sector.venue, name="Standard", color="#00aa00")
    return premium, standard


@pytest.fixture
def seats(sector: VenueSector, categories: tuple[PriceCategory, PriceCategory]) -> list[VenueSeat]:
    """A1 Premium, A2 Standard, A3 unpainted."""
    premium, standard = categories
    painted: list[PriceCategory | None] = [premium, standard, None]
    return [
        VenueSeat.objects.create(
            sector=sector,
            label=f"A{i + 1}",
            row_label="A",
            number=i + 1,
            adjacency_index=i,
            is_active=True,
            default_price_category=category,
        )
        for i, category in enumerate(painted)
    ]


@pytest.fixture
def offline_tier(
    event: Event, sector: VenueSector, categories: tuple[PriceCategory, PriceCategory], seats: list[VenueSeat]
) -> TicketTier:
    """Category-priced OFFLINE tier: Premium 80, Standard 30, unpainted 50."""
    premium, standard = categories
    return TicketTier.objects.create(
        event=event,
        name="Stalls",
        price=FLAT,
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
        total_quantity=50,
        max_tickets_per_user=5,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.USER_CHOICE,
        venue=sector.venue,
        sector=sector,
        category_prices={str(premium.pk): str(PREMIUM), str(standard.pk): str(STANDARD)},
    )


def _ticket(
    event: Event,
    tier: TicketTier,
    user: RevelUser,
    seat: VenueSeat | None,
    *,
    price_paid: Decimal | None,
    status: Ticket.TicketStatus = Ticket.TicketStatus.ACTIVE,
) -> Ticket:
    return Ticket.objects.create(
        event=event,
        tier=tier,
        user=user,
        guest_name=user.get_display_name(),
        status=status,
        seat=seat,
        sector=seat.sector if seat else None,
        venue=tier.venue,
        price_paid=price_paid,
    )


# ===========================================================================
# Part 1 — the mutation path
# ===========================================================================


class TestUnconfirmConfirmRoundTrip:
    """The admin cycle that used to destroy a server-resolved price."""

    def test_unconfirm_then_confirm_preserves_the_resolved_category_price(
        self, event: Event, offline_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """An 80.00 Premium seat must still be 80.00 after unconfirm → confirm.

        Before the fix ``unconfirm`` nulled ``price_paid`` and ``confirm`` refused to
        restore it (non-PWYC), leaving an ACTIVE ticket priced at the 50.00 flat rate
        everywhere downstream.
        """
        ticket = _ticket(event, offline_tier, member_user, seats[0], price_paid=PREMIUM)

        unconfirmed = ticket_service.unconfirm_ticket_payment(ticket)
        assert unconfirmed.status == Ticket.TicketStatus.PENDING
        assert unconfirmed.price_paid == PREMIUM

        confirmed = ticket_service.confirm_ticket_payment(unconfirmed)
        assert confirmed.status == Ticket.TicketStatus.ACTIVE
        assert confirmed.price_paid == PREMIUM

    def test_a_discounted_price_survives_the_same_cycle(
        self, event: Event, offline_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """A per-ticket discount is equally unreconstructable from ``tier.price``."""
        discounted = Decimal("72.00")
        ticket = _ticket(event, offline_tier, member_user, seats[0], price_paid=discounted)
        ticket.discount_amount = Decimal("8.00")
        ticket.save(update_fields=["discount_amount"])

        confirmed = ticket_service.confirm_ticket_payment(ticket_service.unconfirm_ticket_payment(ticket))

        assert confirmed.price_paid == discounted

    def test_unconfirm_still_clears_an_admin_entered_pwyc_amount(self, event: Event, member_user: RevelUser) -> None:
        """PWYC is the case the clearing was written for — it must keep working."""
        tier = TicketTier.objects.create(
            event=event,
            name="PWYC",
            price=Decimal("0.00"),
            price_type=TicketTier.PriceType.PWYC,
            payment_method=TicketTier.PaymentMethod.OFFLINE,
            pwyc_min=Decimal("5.00"),
        )
        ticket = _ticket(event, tier, member_user, None, price_paid=Decimal("25.00"))

        assert ticket_service.unconfirm_ticket_payment(ticket).price_paid is None

    def test_a_flat_tier_ticket_is_unaffected(self, event: Event, member_user: RevelUser) -> None:
        """No map, no discount: ``price_paid`` was already NULL and stays NULL."""
        tier = TicketTier.objects.create(
            event=event, name="Flat", price=FLAT, payment_method=TicketTier.PaymentMethod.OFFLINE
        )
        ticket = _ticket(event, tier, member_user, None, price_paid=None)

        assert ticket_service.unconfirm_ticket_payment(ticket).price_paid is None


# ===========================================================================
# Part 2 — the money-bearing fallbacks
# ===========================================================================


class TestRefundCeiling:
    """``_resolve_offline_refund_amount`` caps a manual refund at what was collected."""

    def test_null_price_paid_falls_back_to_the_seat_category_not_the_flat_price(
        self, event: Event, offline_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """A Premium seat with a NULL price refunds up to 80.00, not the 50.00 flat price."""
        ticket = _ticket(event, offline_tier, member_user, seats[0], price_paid=None)
        ticket.refresh_from_db()

        assert ticket_service._resolve_offline_refund_amount(ticket, None) == PREMIUM

    def test_an_explicit_amount_above_the_seat_price_is_still_rejected(
        self, event: Event, offline_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """The ceiling is the resolved price — 30.00 for a Standard seat."""
        ticket = _ticket(event, offline_tier, member_user, seats[1], price_paid=None)
        ticket.refresh_from_db()

        with pytest.raises(HttpError):
            ticket_service._resolve_offline_refund_amount(ticket, Decimal("40.00"))
        assert ticket_service._resolve_offline_refund_amount(ticket, Decimal("30.00")) == STANDARD

    def test_an_unpainted_seat_keeps_the_flat_price_ceiling(
        self, event: Event, offline_tier: TicketTier, seats: list[VenueSeat], member_user: RevelUser
    ) -> None:
        """The documented legitimate fallback — unchanged."""
        ticket = _ticket(event, offline_tier, member_user, seats[2], price_paid=None)
        ticket.refresh_from_db()

        assert ticket_service._resolve_offline_refund_amount(ticket, None) == FLAT

    def test_a_flat_tier_never_warns_and_never_raises(self, event: Event, member_user: RevelUser) -> None:
        """Legacy NULLs on a tier that never opted in are legitimate, not an anomaly."""
        tier = TicketTier.objects.create(
            event=event, name="Flat", price=FLAT, payment_method=TicketTier.PaymentMethod.OFFLINE
        )
        ticket = _ticket(event, tier, member_user, None, price_paid=None)

        assert ticket_service._resolve_offline_refund_amount(ticket, None) == FLAT


class TestRevenueAggregationGross:
    """The offline gross in the revenue/VAT report."""

    @staticmethod
    def _scope(organization: Organization) -> ReportScope:
        today = timezone.localdate()
        return ReportScope(
            org=organization, event_id=None, date_from=today - timedelta(days=1), date_to=today + timedelta(days=1)
        )

    def test_mixed_offline_cart_reports_each_seat_at_its_own_price(
        self,
        organization: Organization,
        event: Event,
        offline_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """80 + 30 + 50 = 160.00 gross — not 3 × the 50.00 flat price."""
        for seat in seats:
            _ticket(event, offline_tier, member_user, seat, price_paid=None)

        report = build_revenue_report_data(self._scope(organization))

        section = next(s for s in report.sections if s.currency == "EUR")
        assert sum(bucket.gross for bucket in section.rate_buckets) == Decimal("160.00")
        assert sorted(row.gross for row in section.transactions) == [STANDARD, FLAT, PREMIUM]

    def test_a_recorded_price_still_wins_over_the_map(
        self,
        organization: Organization,
        event: Event,
        offline_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
    ) -> None:
        """``price_paid`` is purchase-time truth; a later repricing must not rewrite history."""
        _ticket(event, offline_tier, member_user, seats[0], price_paid=Decimal("70.00"))

        report = build_revenue_report_data(self._scope(organization))

        section = next(s for s in report.sections if s.currency == "EUR")
        assert sum(bucket.gross for bucket in section.rate_buckets) == Decimal("70.00")

    def test_resolving_the_seat_price_does_not_go_n_plus_1(
        self,
        organization: Organization,
        event: Event,
        offline_tier: TicketTier,
        seats: list[VenueSeat],
        member_user: RevelUser,
        categories: tuple[PriceCategory, PriceCategory],
        sector: VenueSector,
    ) -> None:
        """Six tickets must cost the same number of queries as three."""
        scope = self._scope(organization)
        for seat in seats:
            _ticket(event, offline_tier, member_user, seat, price_paid=None)
        with CaptureQueriesContext(connection) as first:
            build_revenue_report_data(scope)

        premium, standard = categories
        for i, category in enumerate([premium, standard, None]):
            seat = VenueSeat.objects.create(
                sector=sector,
                label=f"B{i + 1}",
                row_label="B",
                number=i + 1,
                adjacency_index=10 + i,
                default_price_category=category,
            )
            _ticket(event, offline_tier, member_user, seat, price_paid=None)
        with CaptureQueriesContext(connection) as second:
            build_revenue_report_data(scope)

        assert len(second) == len(first)
