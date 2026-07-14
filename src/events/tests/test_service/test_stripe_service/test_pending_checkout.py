"""Tests for pending checkout resume and cancel functions."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    Payment,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.service import stripe_service
from events.service.series_pass_purchase import SeriesPassPurchaseService

pytestmark = pytest.mark.django_db


class TestResumePendingCheckout:
    """Tests for resume_pending_checkout function."""

    @pytest.fixture
    def pending_payment(
        self,
        event: Event,
        organization_owner_user: RevelUser,
    ) -> Payment:
        """Create a pending payment for testing."""
        tier = event.ticket_tiers.first()
        assert tier is not None
        tier.quantity_sold = 1
        tier.save()
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Test Guest",
        )
        return Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_test_resume",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

    def test_returns_404_when_payment_not_found(
        self,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return 404 when payment doesn't exist."""
        with pytest.raises(HttpError) as exc_info:
            stripe_service.resume_pending_checkout("00000000-0000-0000-0000-000000000000", organization_owner_user)
        assert exc_info.value.status_code == 404
        assert "No pending payment found" in str(exc_info.value.message)

    def test_returns_404_when_payment_not_owned_by_user(
        self,
        pending_payment: Payment,
        member_user: RevelUser,
    ) -> None:
        """Should return 404 when payment belongs to different user."""
        with pytest.raises(HttpError) as exc_info:
            stripe_service.resume_pending_checkout(str(pending_payment.id), member_user)
        assert exc_info.value.status_code == 404

    def test_returns_404_when_payment_expired(
        self,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return 404 and clean up when payment has expired."""
        # Make payment expired
        pending_payment.expires_at = timezone.now() - timedelta(hours=1)
        pending_payment.save()
        ticket_id = pending_payment.ticket_id

        with pytest.raises(HttpError) as exc_info:
            stripe_service.resume_pending_checkout(str(pending_payment.id), organization_owner_user)

        assert exc_info.value.status_code == 404
        assert "expired" in str(exc_info.value.message).lower()
        # Verify cleanup
        assert not Payment.objects.filter(id=pending_payment.id).exists()
        assert not Ticket.objects.filter(id=ticket_id).exists()

    @patch("stripe.checkout.Session.retrieve")
    def test_returns_checkout_url_for_valid_payment(
        self,
        mock_session_retrieve: Mock,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return Stripe checkout URL for valid pending payment."""
        mock_session_retrieve.return_value = Mock(url="https://checkout.stripe.com/test")

        result = stripe_service.resume_pending_checkout(str(pending_payment.id), organization_owner_user)

        assert result == "https://checkout.stripe.com/test"
        mock_session_retrieve.assert_called_once()


class TestResumeUnsessionedReservation:
    """resume_pending_checkout on a reserved-but-not-sessioned payment (#632).

    Since #632, a reserve can leave a PENDING Payment with stripe_session_id=""
    (the /checkout-session step hasn't run yet). resume must create the session
    now instead of calling Session.retrieve("") and 404ing a live reservation.
    """

    @pytest.fixture
    def stripe_connected_organization(self, organization: Organization) -> Organization:
        """Organization with Stripe account connected."""
        organization.stripe_account_id = "acct_test_resume"
        organization.stripe_charges_enabled = True
        organization.stripe_details_submitted = True
        organization.platform_fee_percent = Decimal("3.00")
        organization.platform_fee_fixed = Decimal("0.50")
        organization.save()
        return organization

    @pytest.fixture
    def paid_ticket_tier(self, event: Event, stripe_connected_organization: Organization) -> TicketTier:
        """A paid ticket tier on a Stripe-connected event."""
        event.organization = stripe_connected_organization
        event.save()
        tier = event.ticket_tiers.first()
        assert tier is not None
        tier.price = Decimal("25.00")
        tier.total_quantity = 10
        tier.save()
        return tier

    def test_resume_unsessioned_batch_creates_session(
        self,
        event: Event,
        paid_ticket_tier: TicketTier,
        organization_owner_user: RevelUser,
    ) -> None:
        """Resuming an un-sessioned batch reserve creates the session instead of 404ing."""
        ticket = Ticket.objects.create(
            event=event,
            tier=paid_ticket_tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name="A",
        )
        rid = uuid4()
        stripe_service.reserve_batch_payments(
            event=event,
            tier=paid_ticket_tier,
            user=organization_owner_user,
            tickets=[ticket],
            reservation_id=rid,
        )
        payment = Payment.objects.get(reservation_id=rid)
        assert payment.stripe_session_id == ""

        fake_session = Mock(id="cs_resume_batch", url="https://checkout.stripe.com/c/cs_resume_batch")
        with (
            patch("stripe.checkout.Session.create", return_value=fake_session) as create,
            patch("stripe.checkout.Session.retrieve") as retrieve,
        ):
            result = stripe_service.resume_pending_checkout(str(payment.id), organization_owner_user)

        assert result == fake_session.url
        create.assert_called_once()
        retrieve.assert_not_called()
        payment.refresh_from_db()
        assert payment.stripe_session_id == "cs_resume_batch"

    def test_resume_unsessioned_series_pass_creates_session(
        self,
        stripe_connected_organization: Organization,
        event_series: EventSeries,
        member_user: RevelUser,
    ) -> None:
        """Resuming an un-sessioned series-pass reserve creates the session via the series path."""
        series_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="Resume Pass",
            price=Decimal("10.00"),
            pro_rata_discount=Decimal("0.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        # get_quote requires >=2 remaining events for a pass to be purchasable.
        for i in range(2):
            future_event = Event.objects.create(
                organization=stripe_connected_organization,
                name=f"Resume Future {i}",
                slug=f"resume-future-{i}",
                event_type=Event.EventType.PUBLIC,
                visibility=Event.Visibility.PUBLIC,
                event_series=event_series,
                max_attendees=100,
                start=timezone.now() + timedelta(days=i + 1),
                status=Event.EventStatus.OPEN,
                requires_ticket=True,
            )
            tier = TicketTier.objects.create(
                event=future_event,
                name=f"Resume Tier {i}",
                price=Decimal("10.00"),
                currency="EUR",
                payment_method=TicketTier.PaymentMethod.ONLINE,
            )
            SeriesPassTierLink.objects.create(series_pass=series_pass, event=future_event, tier=tier)

        with patch("stripe.checkout.Session.create") as mock_create_reserve:
            held_pass, reservation_id = SeriesPassPurchaseService(series_pass, member_user).purchase()  # type: ignore[misc]
            mock_create_reserve.assert_not_called()

        payments = list(Payment.objects.filter(reservation_id=reservation_id))
        assert len(payments) == 2
        assert all(p.stripe_session_id == "" for p in payments)
        assert all(p.ticket.held_pass_id == held_pass.id for p in payments)

        fake_session = Mock(id="cs_resume_series", url="https://checkout.stripe.com/c/cs_resume_series")
        with (
            patch("stripe.checkout.Session.create", return_value=fake_session) as create,
            patch("stripe.checkout.Session.retrieve") as retrieve,
        ):
            result = stripe_service.resume_pending_checkout(str(payments[0].id), member_user)

        assert result == fake_session.url
        create.assert_called_once()
        retrieve.assert_not_called()
        held_pass.refresh_from_db()
        assert held_pass.stripe_session_id == "cs_resume_series"


class TestCancelPendingCheckout:
    """Tests for cancel_pending_checkout function."""

    @pytest.fixture
    def pending_payment(
        self,
        event: Event,
        organization_owner_user: RevelUser,
    ) -> Payment:
        """Create a pending payment for testing."""
        tier = event.ticket_tiers.first()
        assert tier is not None
        tier.quantity_sold = 1
        tier.save()
        ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=organization_owner_user,
            status=Ticket.TicketStatus.PENDING,
            guest_name="Test Guest",
        )
        return Payment.objects.create(
            ticket=ticket,
            user=organization_owner_user,
            stripe_session_id="cs_test_cancel",
            amount=Decimal("25.00"),
            platform_fee=Decimal("1.25"),
            currency="EUR",
            status=Payment.PaymentStatus.PENDING,
            raw_response={},
        )

    def test_returns_404_when_payment_not_found(
        self,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return 404 when payment doesn't exist."""
        with pytest.raises(HttpError) as exc_info:
            stripe_service.cancel_pending_checkout("00000000-0000-0000-0000-000000000000", organization_owner_user)
        assert exc_info.value.status_code == 404
        assert "Payment not found" in str(exc_info.value.message)

    def test_returns_404_when_payment_not_owned_by_user(
        self,
        pending_payment: Payment,
        member_user: RevelUser,
    ) -> None:
        """Should return 404 when payment belongs to different user."""
        with pytest.raises(HttpError) as exc_info:
            stripe_service.cancel_pending_checkout(str(pending_payment.id), member_user)
        assert exc_info.value.status_code == 404

    def test_returns_400_when_payment_not_pending(
        self,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should return 400 when payment is not in PENDING status."""
        pending_payment.status = Payment.PaymentStatus.SUCCEEDED
        pending_payment.save()

        with pytest.raises(HttpError) as exc_info:
            stripe_service.cancel_pending_checkout(str(pending_payment.id), organization_owner_user)
        assert exc_info.value.status_code == 400
        assert "Only pending payments" in str(exc_info.value.message)

    def test_deletes_payment_and_ticket(
        self,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should delete payment and ticket on successful cancel."""
        payment_id = pending_payment.id
        ticket_id = pending_payment.ticket_id

        result = stripe_service.cancel_pending_checkout(str(pending_payment.id), organization_owner_user)

        assert result == 1
        assert not Payment.objects.filter(id=payment_id).exists()
        assert not Ticket.objects.filter(id=ticket_id).exists()

    def test_decrements_quantity_sold(
        self,
        pending_payment: Payment,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should decrement tier's quantity_sold."""
        tier = pending_payment.ticket.tier
        initial_sold = tier.quantity_sold

        stripe_service.cancel_pending_checkout(str(pending_payment.id), organization_owner_user)

        tier.refresh_from_db()
        assert tier.quantity_sold == initial_sold - 1

    def test_deletes_all_tickets_in_batch(
        self,
        event: Event,
        organization_owner_user: RevelUser,
    ) -> None:
        """Should delete all tickets with same stripe_session_id."""
        tier = event.ticket_tiers.first()
        assert tier is not None
        tier.quantity_sold = 3
        tier.save()

        # Create 3 tickets with same session_id (batch purchase)
        session_id = "cs_test_batch"
        tickets = []
        payments = []
        for i in range(3):
            ticket = Ticket.objects.create(
                event=event,
                tier=tier,
                user=organization_owner_user,
                status=Ticket.TicketStatus.PENDING,
                guest_name=f"Guest {i}",
            )
            tickets.append(ticket)
            payment = Payment.objects.create(
                ticket=ticket,
                user=organization_owner_user,
                stripe_session_id=session_id,
                amount=Decimal("25.00"),
                platform_fee=Decimal("1.25"),
                currency="EUR",
                status=Payment.PaymentStatus.PENDING,
                raw_response={},
            )
            payments.append(payment)

        # Cancel using any payment in the batch
        result = stripe_service.cancel_pending_checkout(str(payments[0].id), organization_owner_user)

        assert result == 3
        # All tickets and payments deleted
        for ticket in tickets:
            assert not Ticket.objects.filter(id=ticket.id).exists()
        for payment in payments:
            assert not Payment.objects.filter(id=payment.id).exists()
        # quantity_sold decremented by 3
        tier.refresh_from_db()
        assert tier.quantity_sold == 0
