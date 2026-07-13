"""Tests for the series-pass reserve/session split (#632)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from uuid import uuid4

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
    held_pass: HeldSeriesPass,
    count: int = 5,
) -> list[Ticket]:
    """Create `count` PENDING tickets, each on its own event/tier, split across two VAT rates.

    Linked to ``held_pass`` — ``create_series_pass_session`` derives the held pass from
    ``tickets[0].held_pass`` (mirrors production, where ``materialize_tickets`` always
    sets it).
    """
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
            held_pass=held_pass,
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
        status=HeldSeriesPass.HeldSeriesPassStatus.PENDING,
    )


@pytest.fixture
def tickets(
    organization: Organization, event_series: EventSeries, revel_user: RevelUser, held_pass: HeldSeriesPass
) -> list[Ticket]:
    return _make_tiered_tickets(organization, event_series, revel_user, held_pass)


class TestReserveSeriesPassPayments:
    def test_raises_error_when_organization_not_connected(
        self, held_pass: HeldSeriesPass, tickets: list[Ticket]
    ) -> None:
        with pytest.raises(HttpError) as exc_info:
            stripe_service.reserve_series_pass_payments(held_pass=held_pass, tickets=tickets, reservation_id=uuid4())

        assert exc_info.value.status_code == 400
        assert "not configured to accept payments" in exc_info.value.message

    def test_raises_error_for_non_positive_price(
        self,
        stripe_connected_organization: Organization,
        online_series_pass: SeriesPass,
        organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
    ) -> None:
        held_pass = HeldSeriesPass.objects.create(
            series_pass=online_series_pass,
            user=revel_user,
            price_paid=Decimal("0.00"),
            status=HeldSeriesPass.HeldSeriesPassStatus.PENDING,
        )
        tickets = _make_tiered_tickets(organization, event_series, revel_user, held_pass)

        with pytest.raises(HttpError) as exc_info:
            stripe_service.reserve_series_pass_payments(held_pass=held_pass, tickets=tickets, reservation_id=uuid4())

        assert exc_info.value.status_code == 400
        assert "cannot be purchased" in exc_info.value.message

    def test_creates_n_pending_payment_rows_summing_penny_exact_no_stripe_call(
        self,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
    ) -> None:
        reservation_id = uuid4()
        with patch("stripe.checkout.Session.create") as mock_create:
            stripe_service.reserve_series_pass_payments(
                held_pass=held_pass, tickets=tickets, reservation_id=reservation_id
            )
            mock_create.assert_not_called()

        payments = list(Payment.objects.filter(reservation_id=reservation_id))
        assert len(payments) == 5
        assert all(p.status == Payment.PaymentStatus.PENDING for p in payments)
        assert all(p.stripe_session_id == "" for p in payments)
        assert sum(p.amount for p in payments) == held_pass.price_paid

    def test_per_row_vat_rate_matches_each_tickets_tier(
        self,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
    ) -> None:
        stripe_service.reserve_series_pass_payments(held_pass=held_pass, tickets=tickets, reservation_id=uuid4())

        for ticket in tickets:
            payment = Payment.objects.get(ticket=ticket)
            assert payment.vat_rate == ticket.tier.vat_rate


class TestCreateSeriesPassSession:
    def test_no_pending_reservation_raises_404(self) -> None:
        with pytest.raises(HttpError) as exc_info:
            stripe_service.create_series_pass_session(reservation_id=uuid4())

        assert exc_info.value.status_code == 404

    def test_expired_reservation_raises_404(
        self,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
    ) -> None:
        reservation_id = uuid4()
        stripe_service.reserve_series_pass_payments(held_pass=held_pass, tickets=tickets, reservation_id=reservation_id)
        Payment.objects.filter(reservation_id=reservation_id).update(expires_at=timezone.now() - timedelta(minutes=1))

        with pytest.raises(HttpError) as exc_info:
            stripe_service.create_series_pass_session(reservation_id=reservation_id)

        assert exc_info.value.status_code == 404

    @patch("stripe.checkout.Session.create")
    def test_creates_session_and_stamps_payments_and_held_pass(
        self,
        mock_stripe_create: Mock,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
    ) -> None:
        reservation_id = uuid4()
        stripe_service.reserve_series_pass_payments(held_pass=held_pass, tickets=tickets, reservation_id=reservation_id)

        mock_session = Mock()
        mock_session.id = "cs_series_test"
        mock_session.url = "https://checkout.stripe.com/pay/cs_series_test"
        mock_stripe_create.return_value = mock_session

        url = stripe_service.create_series_pass_session(reservation_id=reservation_id)

        assert url == mock_session.url
        payments = list(Payment.objects.filter(reservation_id=reservation_id))
        assert len(payments) == 5
        assert all(p.stripe_session_id == "cs_series_test" for p in payments)
        assert sum(p.amount for p in payments) == held_pass.price_paid
        held_pass.refresh_from_db()
        assert held_pass.stripe_session_id == "cs_series_test"

    @patch("stripe.checkout.Session.create")
    def test_passes_reservation_id_as_idempotency_key(
        self,
        mock_stripe_create: Mock,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
    ) -> None:
        reservation_id = uuid4()
        stripe_service.reserve_series_pass_payments(held_pass=held_pass, tickets=tickets, reservation_id=reservation_id)
        mock_stripe_create.return_value = Mock(id="cs_idem", url="https://checkout.stripe.com/pay/cs_idem")

        stripe_service.create_series_pass_session(reservation_id=reservation_id)

        assert mock_stripe_create.call_args[1]["idempotency_key"] == str(reservation_id)

    @patch("stripe.checkout.Session.create")
    def test_session_metadata_and_line_item(
        self,
        mock_stripe_create: Mock,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
        online_series_pass: SeriesPass,
    ) -> None:
        reservation_id = uuid4()
        stripe_service.reserve_series_pass_payments(held_pass=held_pass, tickets=tickets, reservation_id=reservation_id)
        mock_session = Mock()
        mock_session.id = "cs_series_meta"
        mock_session.url = "https://checkout.stripe.com/pay/cs_series_meta"
        mock_stripe_create.return_value = mock_session

        stripe_service.create_series_pass_session(reservation_id=reservation_id)

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

    @patch("stripe.checkout.Session.retrieve")
    @patch("stripe.checkout.Session.create")
    def test_already_sessioned_returns_existing_url_without_recreating(
        self,
        mock_stripe_create: Mock,
        mock_stripe_retrieve: Mock,
        stripe_connected_organization: Organization,
        held_pass: HeldSeriesPass,
        tickets: list[Ticket],
    ) -> None:
        reservation_id = uuid4()
        stripe_service.reserve_series_pass_payments(held_pass=held_pass, tickets=tickets, reservation_id=reservation_id)
        mock_session = Mock()
        mock_session.id = "cs_first"
        mock_session.url = "https://checkout.stripe.com/pay/cs_first"
        mock_stripe_create.return_value = mock_session

        first_url = stripe_service.create_series_pass_session(reservation_id=reservation_id)
        assert first_url == mock_session.url

        mock_stripe_create.reset_mock()
        mock_stripe_retrieve.return_value = Mock(url=mock_session.url)

        second_url = stripe_service.create_series_pass_session(reservation_id=reservation_id)

        mock_stripe_create.assert_not_called()
        assert second_url == mock_session.url


class TestSeriesPassPurchaseServiceOnlinePath:
    @patch("stripe.checkout.Session.create")
    def test_purchase_reserves_then_session_returns_url_leaving_pending_state(
        self,
        mock_stripe_create: Mock,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        revel_user: RevelUser,
    ) -> None:
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

        mock_stripe_create.assert_not_called()
        assert isinstance(result, tuple)
        held_pass, reservation_id = result
        assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.PENDING
        tickets = list(Ticket.objects.filter(held_pass=held_pass))
        assert len(tickets) == 3
        assert all(t.status == Ticket.TicketStatus.PENDING for t in tickets)
        payments = list(Payment.objects.filter(ticket__in=tickets))
        assert len(payments) == 3
        assert all(p.status == Payment.PaymentStatus.PENDING for p in payments)
        assert all(p.reservation_id == reservation_id for p in payments)
        assert all(p.stripe_session_id == "" for p in payments)

        url = stripe_service.create_series_pass_session(reservation_id=reservation_id)

        assert url == mock_session.url
        mock_stripe_create.assert_called_once()
        held_pass.refresh_from_db()
        assert held_pass.stripe_session_id == "cs_e2e"
        for payment in payments:
            payment.refresh_from_db()
            assert payment.stripe_session_id == "cs_e2e"
