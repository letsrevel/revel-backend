"""Tests for bootstrap seeding helpers."""

from datetime import date
from decimal import Decimal

import pytest

from accounts.models import GlobalBan, RevelUser
from common.models import SiteSettings
from events.management.commands.bootstrap_helpers.billing import _create_bootstrap_payments
from events.management.commands.bootstrap_helpers.users import create_global_bans
from events.models import Event, Ticket, TicketTier, VenueSeat

pytestmark = pytest.mark.django_db


class TestCreateGlobalBans:
    """Coverage for ``create_global_bans``."""

    def test_seeds_email_and_domain_bans(self) -> None:
        create_global_bans()

        assert GlobalBan.objects.filter(ban_type=GlobalBan.BanType.EMAIL, value="banned.user@example.com").exists()
        assert GlobalBan.objects.filter(ban_type=GlobalBan.BanType.DOMAIN, value="banned.example").exists()

    def test_is_idempotent(self) -> None:
        """Regression for issue #665.

        ``reset_events`` re-runs ``bootstrap_events`` without clearing GlobalBan
        rows, so re-seeding previously collided with the ``(ban_type,
        normalized_value)`` uniqueness constraint and raised ``ValidationError``,
        leaving the DB half-wiped. Seeding must be idempotent.
        """
        create_global_bans()
        create_global_bans()  # must not raise

        assert GlobalBan.objects.filter(ban_type=GlobalBan.BanType.EMAIL).count() == 1
        assert GlobalBan.objects.filter(ban_type=GlobalBan.BanType.DOMAIN).count() == 1


class TestCreateBootstrapPaymentsSeats:
    """Bootstrap guest tickets on a seated tier must occupy free seats like real purchases."""

    def _run(self, event: Event, tier: TicketTier, users: list[RevelUser]) -> int:
        site = SiteSettings.get_solo()
        site.platform_vat_country = "IT"
        site.platform_vat_rate = Decimal("22.00")
        site.save()
        return _create_bootstrap_payments(
            users=users,
            event=event,
            tier=tier,
            org=event.organization,
            site=site,
            effective_vat_rate=Decimal("20.00"),
            first_of_previous=date(2026, 7, 1),
            last_of_previous=date(2026, 7, 31),
        )

    @pytest.fixture
    def guests(self, django_user_model: type[RevelUser]) -> list[RevelUser]:
        return [
            django_user_model.objects.create_user(
                username=f"bootstrap_payer_{i}", email=f"bootstrap_payer_{i}@example.com", password="pass"
            )
            for i in range(3)
        ]

    def test_seated_tier_tickets_get_free_seats(
        self, seated_event: tuple[Event, list[VenueSeat]], guests: list[RevelUser]
    ) -> None:
        """Tickets take the first free seats, skipping seats already occupied."""
        event, seats = seated_event
        sector = seats[0].sector
        tier = TicketTier.objects.create(
            event=event, name="Seated", price=Decimal("10.00"), venue=event.venue, sector=sector
        )
        # Occupy A1 so the helper must skip it.
        Ticket.objects.create(event=event, user=guests[0], tier=tier, status=Ticket.TicketStatus.ACTIVE, seat=seats[0])

        created = self._run(event, tier, guests)

        assert created == 3
        bootstrap_tickets = Ticket.objects.filter(event=event, guest_name__startswith="Bootstrap Guest").order_by(
            "guest_name"
        )
        assert [t.seat.label for t in bootstrap_tickets if t.seat] == ["A2", "A3", "A4"]
        for ticket in bootstrap_tickets:
            assert ticket.venue_id == event.venue_id
            assert ticket.sector_id == sector.id

    def test_ga_tier_tickets_have_no_seat(
        self, seated_event: tuple[Event, list[VenueSeat]], guests: list[RevelUser]
    ) -> None:
        """A tier without a sector keeps seatless tickets (GA behavior unchanged)."""
        event, _seats = seated_event
        tier = TicketTier.objects.create(event=event, name="GA", price=Decimal("10.00"))

        created = self._run(event, tier, guests)

        assert created == 3
        assert not Ticket.objects.filter(event=event, seat__isnull=False).exists()
