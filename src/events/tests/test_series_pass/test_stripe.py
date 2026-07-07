"""Tests for Stripe checkout session creation for series passes."""

from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.conf import settings
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    Payment,
    SeriesPass,
    Ticket,
    TicketTier,
)
from events.service import stripe_service
from events.service.series_pass_purchase import SeriesPassPurchaseService

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_connected_organization(organization: Organization) -> Organization:
    """Organization with Stripe account connected."""
    organization.stripe_account_id = "acct_test123"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.platform_fee_percent = Decimal("3.00")
    organization.platform_fee_fixed = Decimal("0.50")
    organization.save()
    return organization


@pytest.fixture
def online_series_pass(event_series: EventSeries) -> SeriesPass:
    """An ONLINE series pass with a price that doesn't divide evenly across 5 tickets."""
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Season Ticket",
        price=Decimal("33.33"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )


def _make_tiered_tickets(
    organization: Organization,
    event_series: EventSeries,
    user: RevelUser,
    count: int = 5,
) -> list[Ticket]:
    """Create `count` PENDING tickets, each on its own event/tier, split across two VAT rates."""
    tickets = []
    for i in range(count):
        event = Event.objects.create(
            organization=organization,
            name=f"Event {i}",
            slug=f"event-{i}",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            event_series=event_series,
            max_attendees=100,
            start=timezone.now(),
            status=Event.EventStatus.OPEN,
            requires_ticket=True,
        )
        vat_rate = Decimal("10.00") if i < 2 else Decimal("22.00")
        tier = TicketTier.objects.create(
            event=event,
            name=f"Tier {i}",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
            vat_rate=vat_rate,
        )
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=user,
            status=Ticket.TicketStatus.PENDING,
            guest_name=user.get_display_name(),
        )
        tickets.append(ticket)
    return tickets


@pytest.fixture
def held_pass(online_series_pass: SeriesPass, revel_user: RevelUser) -> HeldSeriesPass:
    return HeldSeriesPass.objects.create(
        series_pass=online_series_pass,
        user=revel_user,
        price_paid=online_series_pass.price,
        status=HeldSeriesPass.Status.PENDING,
    )


@pytest.fixture
def tickets(organization: Organization, event_series: EventSeries, revel_user: RevelUser) -> list[Ticket]:
    return _make_tiered_tickets(organization, event_series, revel_user)


class TestCreateSeriesPassCheckoutSession:
    def test_raises_error_when_organization_not_connected(
        self, held_pass: HeldSeriesPass, tickets: list[Ticket]
    ) -> None:
        with pytest.raises(HttpError) as exc_info:
            stripe_service.create_series_pass_checkout_session(held_pass=held_pass, tickets=tickets)

        assert exc_info.value.status_code == 400
        assert "not configured to accept payments" in exc_info.value.message

    def test_raises_error_for_non_positive_price(
        self,
        stripe_connected_organization: Organization,
        online_series_pass: SeriesPass,
        revel_user: RevelUser,
        tickets: list[Ticket],
    ) -> None:
        held_pass = HeldSeriesPass.objects.create(
            series_pass=online_series_pass,
            user=revel_user,
            price_paid=Decimal("0.00"),
            status=HeldSeriesPass.Status.PENDING,
        )

        with pytest.raises(HttpError) as exc_info:
            stripe_service.create_series_pass_checkout_session(held_pass=held_pass, tickets=tickets)

        assert exc_info.value.status_code == 400
        assert "cannot be purchased" in exc_info.value.message

    @patch("stripe.checkout.Session.create")
    def test_creates_n_payment_rows_summing_penny_exact(
        self,
        mock_stripe_create: Mock,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
    ) -> None:
        mock_session = Mock()
        mock_session.id = "cs_series_test"
        mock_session.url = "https://checkout.stripe.com/pay/cs_series_test"
        mock_stripe_create.return_value = mock_session

        stripe_service.create_series_pass_checkout_session(held_pass=held_pass, tickets=tickets)

        payments = list(Payment.objects.filter(stripe_session_id="cs_series_test"))
        assert len(payments) == 5
        assert sum(p.amount for p in payments) == held_pass.price_paid

    @patch("stripe.checkout.Session.create")
    def test_per_row_vat_rate_matches_each_tickets_tier(
        self,
        mock_stripe_create: Mock,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
    ) -> None:
        mock_session = Mock()
        mock_session.id = "cs_series_vat"
        mock_session.url = "https://checkout.stripe.com/pay/cs_series_vat"
        mock_stripe_create.return_value = mock_session

        stripe_service.create_series_pass_checkout_session(held_pass=held_pass, tickets=tickets)

        for ticket in tickets:
            payment = Payment.objects.get(ticket=ticket)
            assert payment.vat_rate == ticket.tier.vat_rate

    @patch("stripe.checkout.Session.create")
    def test_session_metadata_and_line_item(
        self,
        mock_stripe_create: Mock,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
        online_series_pass: SeriesPass,
    ) -> None:
        mock_session = Mock()
        mock_session.id = "cs_series_meta"
        mock_session.url = "https://checkout.stripe.com/pay/cs_series_meta"
        mock_stripe_create.return_value = mock_session

        stripe_service.create_series_pass_checkout_session(held_pass=held_pass, tickets=tickets)

        call_args = mock_stripe_create.call_args
        assert call_args[1]["metadata"]["held_pass_id"] == str(held_pass.id)
        assert call_args[1]["metadata"]["user_id"] == str(held_pass.user_id)
        assert call_args[1]["metadata"]["ticket_ids"] == ",".join(str(t.id) for t in tickets)

        assert len(call_args[1]["line_items"]) == 1
        line_item = call_args[1]["line_items"][0]
        assert line_item["price_data"]["unit_amount"] == 3333
        series = online_series_pass.event_series
        expected_name = f"Season pass: {online_series_pass.name} — {series.name}"
        assert line_item["price_data"]["product_data"]["name"] == expected_name

        series_base_url = f"{settings.FRONTEND_BASE_URL}/events/{series.organization.slug}/series/{series.slug}"
        assert call_args[1]["success_url"] == f"{series_base_url}?payment_success=true"
        assert call_args[1]["cancel_url"] == f"{series_base_url}?payment_cancelled=true"

    @patch("stripe.checkout.Session.create")
    def test_persists_stripe_session_id_on_held_pass(
        self,
        mock_stripe_create: Mock,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
    ) -> None:
        mock_session = Mock()
        mock_session.id = "cs_series_persist"
        mock_session.url = "https://checkout.stripe.com/pay/cs_series_persist"
        mock_stripe_create.return_value = mock_session

        stripe_service.create_series_pass_checkout_session(held_pass=held_pass, tickets=tickets)

        held_pass.refresh_from_db()
        assert held_pass.stripe_session_id == "cs_series_persist"


class TestSeriesPassPurchaseServiceOnlinePath:
    @patch("stripe.checkout.Session.create")
    def test_purchase_returns_url_and_leaves_pending_state(
        self,
        mock_stripe_create: Mock,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
    ) -> None:
        from datetime import timedelta

        from events.models import SeriesPassTierLink

        mock_session = Mock()
        mock_session.id = "cs_e2e"
        mock_session.url = "https://checkout.stripe.com/pay/cs_e2e"
        mock_stripe_create.return_value = mock_session

        series_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="E2E Pass",
            price=Decimal("30.00"),
            pro_rata_discount=Decimal("5.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        now = timezone.now()
        for i in range(3):
            event = Event.objects.create(
                organization=stripe_connected_organization,
                name=f"Future {i}",
                slug=f"future-{i}",
                event_type=Event.EventType.PUBLIC,
                visibility=Event.Visibility.PUBLIC,
                event_series=event_series,
                max_attendees=100,
                start=now + timedelta(days=i + 1),
                status=Event.EventStatus.OPEN,
                requires_ticket=True,
            )
            tier = TicketTier.objects.create(
                event=event,
                name=f"Tier {i}",
                price=Decimal("10.00"),
                currency="EUR",
                payment_method=TicketTier.PaymentMethod.ONLINE,
            )
            SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)

        result = SeriesPassPurchaseService(series_pass, revel_user).purchase()

        assert result == "https://checkout.stripe.com/pay/cs_e2e"
        held_pass = HeldSeriesPass.objects.get(series_pass=series_pass, user=revel_user)
        assert held_pass.status == HeldSeriesPass.Status.PENDING
        tickets = list(Ticket.objects.filter(held_pass=held_pass))
        assert len(tickets) == 3
        assert all(t.status == Ticket.TicketStatus.PENDING for t in tickets)
        payments = list(Payment.objects.filter(ticket__in=tickets))
        assert len(payments) == 3
        assert all(p.status == Payment.PaymentStatus.PENDING for p in payments)
