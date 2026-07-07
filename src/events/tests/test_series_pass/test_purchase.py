"""Tests for series pass purchase: free/offline paths, all-or-nothing capacity, eligibility."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.db import IntegrityError
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.exceptions import SeriesPassNotPurchasableError
from events.models import (
    Blacklist,
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationMember,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.service.series_pass_purchase import SeriesPassPurchaseService

pytestmark = pytest.mark.django_db


def _make_event(organization: Organization, event_series: EventSeries, name: str, slug: str, start: t.Any) -> Event:
    return Event.objects.create(
        organization=organization,
        name=name,
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=start,
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


def _make_tier(event: Event, name: str) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name=name,
        price=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


@pytest.fixture
def future_events(organization: Organization, event_series: EventSeries) -> list[Event]:
    now = timezone.now()
    return [
        _make_event(organization, event_series, f"Future {i}", f"future-{i}", now + timedelta(days=i))
        for i in range(1, 4)
    ]


@pytest.fixture
def future_tiers(future_events: list[Event]) -> list[TicketTier]:
    return [_make_tier(event, f"Tier for {event.name}") for event in future_events]


@pytest.fixture
def past_event(organization: Organization, event_series: EventSeries) -> Event:
    now = timezone.now()
    return _make_event(organization, event_series, "Past", "past", now - timedelta(days=1))


@pytest.fixture
def past_tier(past_event: Event) -> TicketTier:
    return _make_tier(past_event, "Past Tier")


@pytest.fixture
def purchasable_free_pass(
    event_series: EventSeries,
    past_event: Event,
    past_tier: TicketTier,
    future_events: list[Event],
    future_tiers: list[TicketTier],
) -> SeriesPass:
    """Free pass covering 1 past + 3 future events (remaining=3, purchasable)."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Free Season Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=past_event, tier=past_tier)
    for event, tier in zip(future_events, future_tiers):
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


@pytest.fixture
def purchasable_offline_pass(
    event_series: EventSeries,
    past_event: Event,
    past_tier: TicketTier,
    future_events: list[Event],
    future_tiers: list[TicketTier],
) -> SeriesPass:
    """Offline pass covering 1 past + 3 future events (remaining=3, purchasable)."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Offline Season Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.OFFLINE,
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=past_event, tier=past_tier)
    for event, tier in zip(future_events, future_tiers):
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


@pytest.fixture
def purchasable_online_pass(
    event_series: EventSeries,
    past_event: Event,
    past_tier: TicketTier,
    future_events: list[Event],
    future_tiers: list[TicketTier],
) -> SeriesPass:
    """Online pass covering 1 past + 3 future events (remaining=3, purchasable)."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Online Season Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=past_event, tier=past_tier)
    for event, tier in zip(future_events, future_tiers):
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


@pytest.fixture
def under_min_pass(organization: Organization, event_series: EventSeries) -> SeriesPass:
    """Pass with 2 past + 1 future event → remaining=1, not purchasable."""
    now = timezone.now()
    p1 = _make_event(organization, event_series, "P1", "p1", now - timedelta(days=2))
    p2 = _make_event(organization, event_series, "P2", "p2", now - timedelta(days=1))
    f1 = _make_event(organization, event_series, "F1", "f1", now + timedelta(days=1))
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Under Min Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    for event in (p1, p2, f1):
        tier = _make_tier(event, f"Tier for {event.name}")
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


class TestFreePathPurchase:
    def test_creates_active_pass_and_tickets_for_future_events_only(
        self, purchasable_free_pass: SeriesPass, revel_user: RevelUser, future_events: list[Event], past_event: Event
    ) -> None:
        held_pass = SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        assert isinstance(held_pass, HeldSeriesPass)
        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.ACTIVE
        assert held_pass.user_id == revel_user.id

        tickets = list(Ticket.objects.filter(held_pass=held_pass))
        assert len(tickets) == 3
        assert {ticket.event_id for ticket in tickets} == {e.id for e in future_events}
        assert all(ticket.status == Ticket.TicketStatus.ACTIVE for ticket in tickets)
        assert not Ticket.objects.filter(held_pass=held_pass, event=past_event).exists()

        expected_guest_name = revel_user.get_full_name() or revel_user.username
        assert all(ticket.guest_name == expected_guest_name for ticket in tickets)

    def test_increments_tier_and_pass_quantity_sold(
        self, purchasable_free_pass: SeriesPass, revel_user: RevelUser, future_tiers: list[TicketTier]
    ) -> None:
        SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        for tier in future_tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 1

        purchasable_free_pass.refresh_from_db()
        assert purchasable_free_pass.quantity_sold == 1

    def test_no_per_ticket_notifications_fired(
        self,
        purchasable_free_pass: SeriesPass,
        revel_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with patch("notifications.signals.ticket.send_batch_ticket_created_notifications") as mock_notify:
            with django_capture_on_commit_callbacks(execute=True):
                SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        mock_notify.assert_not_called()


class TestOfflinePathPurchase:
    def test_creates_pending_pass_and_pending_tickets(
        self, purchasable_offline_pass: SeriesPass, revel_user: RevelUser, future_events: list[Event]
    ) -> None:
        held_pass = SeriesPassPurchaseService(purchasable_offline_pass, revel_user).purchase()

        assert isinstance(held_pass, HeldSeriesPass)
        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.PENDING

        tickets = list(Ticket.objects.filter(held_pass=held_pass))
        assert len(tickets) == 3
        assert all(t.status == Ticket.TicketStatus.PENDING for t in tickets)


class TestOnlinePathPurchase:
    def test_calls_stripe_checkout_and_returns_its_result(
        self, purchasable_online_pass: SeriesPass, revel_user: RevelUser
    ) -> None:
        with patch(
            "events.service.stripe_service.create_series_pass_checkout_session",
            return_value="https://checkout.stripe.com/session/xyz",
        ) as mock_checkout:
            result = SeriesPassPurchaseService(purchasable_online_pass, revel_user).purchase()

        assert result == "https://checkout.stripe.com/session/xyz"
        assert mock_checkout.called
        _, kwargs = mock_checkout.call_args
        held_pass = kwargs["held_pass"]
        assert isinstance(held_pass, HeldSeriesPass)
        tickets = kwargs["tickets"]
        assert len(tickets) == 3
        assert all(ticket.status == Ticket.TicketStatus.PENDING for ticket in tickets)


class TestAllOrNothingCapacity:
    def test_sold_out_future_tier_raises_429_and_persists_nothing(
        self, purchasable_free_pass: SeriesPass, revel_user: RevelUser, future_tiers: list[TicketTier]
    ) -> None:
        sold_out_tier = future_tiers[1]
        sold_out_tier.total_quantity = 1
        sold_out_tier.quantity_sold = 1
        sold_out_tier.save(update_fields=["total_quantity", "quantity_sold"])

        with pytest.raises(HttpError) as exc_info:
            SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        assert exc_info.value.status_code == 429
        assert not HeldSeriesPass.objects.exists()
        assert not Ticket.objects.exists()


class TestEligibility:
    def test_hard_blacklisted_user_raises_403(
        self, purchasable_free_pass: SeriesPass, revel_user: RevelUser, organization: Organization
    ) -> None:
        Blacklist.objects.create(organization=organization, user=revel_user, created_by=organization.owner)

        with pytest.raises(HttpError) as exc_info:
            SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        assert exc_info.value.status_code == 403
        assert not HeldSeriesPass.objects.exists()

    def test_members_only_pass_rejects_non_member(
        self, purchasable_free_pass: SeriesPass, revel_user: RevelUser
    ) -> None:
        purchasable_free_pass.purchasable_by = TicketTier.PurchasableBy.MEMBERS
        purchasable_free_pass.save(update_fields=["purchasable_by"])

        with pytest.raises(HttpError) as exc_info:
            SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        assert exc_info.value.status_code == 403
        assert not HeldSeriesPass.objects.exists()

    def test_members_only_pass_allows_member(
        self, purchasable_free_pass: SeriesPass, revel_user: RevelUser, organization: Organization
    ) -> None:
        purchasable_free_pass.purchasable_by = TicketTier.PurchasableBy.MEMBERS
        purchasable_free_pass.save(update_fields=["purchasable_by"])
        OrganizationMember.objects.create(organization=organization, user=revel_user)

        held_pass = SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        assert isinstance(held_pass, HeldSeriesPass)


class TestNotPurchasable:
    def test_duplicate_purchase_raises_409(self, purchasable_free_pass: SeriesPass, revel_user: RevelUser) -> None:
        HeldSeriesPass.objects.create(
            series_pass=purchasable_free_pass,
            user=revel_user,
            price_paid=Decimal("30.00"),
            status=HeldSeriesPass.Status.ACTIVE,
        )

        with pytest.raises(SeriesPassNotPurchasableError):
            SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        assert HeldSeriesPass.objects.filter(series_pass=purchasable_free_pass, user=revel_user).count() == 1

    def test_not_enough_remaining_events_raises_409(self, under_min_pass: SeriesPass, revel_user: RevelUser) -> None:
        with pytest.raises(SeriesPassNotPurchasableError):
            SeriesPassPurchaseService(under_min_pass, revel_user).purchase()

        assert not HeldSeriesPass.objects.exists()


class TestConcurrentPurchaseRace:
    """Regression tests: a losing concurrent purchase must map to 409, never a raw 500."""

    def test_validation_error_from_full_clean_maps_to_409(
        self, purchasable_free_pass: SeriesPass, revel_user: RevelUser
    ) -> None:
        # Simulate the winner of the race: a real held pass already exists.
        SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        # Bypass the early duplicate precheck once, so the loser reaches HeldSeriesPass.create()
        # and hits full_clean()'s validate_constraints() against the real, already-committed row.
        real_filter = HeldSeriesPass.objects.filter
        call_count = 0

        def fake_filter(*args: object, **kwargs: object) -> t.Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                mock_qs = MagicMock()
                mock_qs.exclude.return_value.exists.return_value = False
                return mock_qs
            return real_filter(*args, **kwargs)

        with patch.object(HeldSeriesPass.objects, "filter", side_effect=fake_filter):
            with pytest.raises(SeriesPassNotPurchasableError):
                SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        assert HeldSeriesPass.objects.filter(series_pass=purchasable_free_pass, user=revel_user).count() == 1

    def test_integrity_error_maps_to_409(self, purchasable_free_pass: SeriesPass, revel_user: RevelUser) -> None:
        with patch.object(
            HeldSeriesPass.objects,
            "create",
            side_effect=IntegrityError("duplicate key value violates unique constraint"),
        ):
            with pytest.raises(SeriesPassNotPurchasableError):
                SeriesPassPurchaseService(purchasable_free_pass, revel_user).purchase()

        assert not HeldSeriesPass.objects.exists()
