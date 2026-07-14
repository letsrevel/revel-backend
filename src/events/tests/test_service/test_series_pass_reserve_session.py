"""Tests for SeriesPassPurchaseService's reserve-only online checkout path (#632).

Covers: online `purchase` reserves (PENDING tickets + PENDING payments, no Stripe
call) and returns `(held_pass, reservation_id)`; `create_series_pass_session`
stamps the payments and the held pass, is idempotent via `idempotency_key`, and
404s for unknown/expired reservations.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest
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
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.service import stripe_service
from events.service.series_pass_purchase import SeriesPassPurchaseService

pytestmark = pytest.mark.django_db


@pytest.fixture
def stripe_connected_organization(organization: Organization) -> Organization:
    """Organization with Stripe account connected."""
    organization.stripe_account_id = "acct_series_reserve"
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save()
    return organization


@pytest.fixture
def online_pass(
    stripe_connected_organization: Organization, event_series: EventSeries
) -> tuple[SeriesPass, list[TicketTier]]:
    """An ONLINE series pass covering 3 future events, one shared tier per event."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Reserve Pass",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    now = timezone.now()
    tiers = []
    for i in range(3):
        event = Event.objects.create(
            organization=stripe_connected_organization,
            name=f"Reserve Future {i}",
            slug=f"reserve-future-{i}",
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
            name=f"Reserve Tier {i}",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
        tiers.append(tier)
    return series_pass, tiers


def test_online_purchase_returns_reservation_and_makes_pending(
    online_pass: tuple[SeriesPass, list[TicketTier]], member_user: RevelUser
) -> None:
    """Online purchase reserves (PENDING tickets + PENDING payments), returns (held_pass, reservation_id), no Stripe."""
    series_pass, _ = online_pass
    with patch("stripe.checkout.Session.create") as mock_create:
        result = SeriesPassPurchaseService(series_pass, member_user).purchase()
        mock_create.assert_not_called()

    assert isinstance(result, tuple)
    held_pass, reservation_id = result
    assert isinstance(held_pass, HeldSeriesPass)
    assert held_pass.status == HeldSeriesPass.HeldSeriesPassStatus.PENDING
    assert held_pass.stripe_session_id == ""

    tickets = list(Ticket.objects.filter(held_pass=held_pass))
    assert len(tickets) == 3
    assert all(tk.status == Ticket.TicketStatus.PENDING for tk in tickets)

    payments = list(Payment.objects.filter(reservation_id=reservation_id))
    assert len(payments) == 3
    assert all(p.status == Payment.PaymentStatus.PENDING for p in payments)
    assert all(p.stripe_session_id == "" for p in payments)
    assert sum(p.amount for p in payments) == held_pass.price_paid


@patch("stripe.checkout.Session.create")
def test_create_series_pass_session_stamps_payments_and_held_pass(
    mock_stripe_create: Mock,
    online_pass: tuple[SeriesPass, list[TicketTier]],
    member_user: RevelUser,
) -> None:
    series_pass, _ = online_pass
    with patch("stripe.checkout.Session.create") as mock_create_reserve:
        held_pass, reservation_id = SeriesPassPurchaseService(series_pass, member_user).purchase()  # type: ignore[misc]
        mock_create_reserve.assert_not_called()

    mock_session = Mock()
    mock_session.id = "cs_reserve_session"
    mock_session.url = "https://checkout.stripe.com/pay/cs_reserve_session"
    mock_stripe_create.return_value = mock_session

    url = stripe_service.create_series_pass_session(reservation_id=reservation_id)

    assert url == mock_session.url
    assert mock_stripe_create.call_args[1]["idempotency_key"] == str(reservation_id)

    held_pass.refresh_from_db()
    assert held_pass.stripe_session_id == "cs_reserve_session"
    payments = list(Payment.objects.filter(reservation_id=reservation_id))
    assert len(payments) == 3
    assert all(p.stripe_session_id == "cs_reserve_session" for p in payments)


def test_create_series_pass_session_unknown_reservation_raises_404() -> None:
    with pytest.raises(HttpError) as exc_info:
        stripe_service.create_series_pass_session(reservation_id=uuid4())

    assert exc_info.value.status_code == 404


def test_create_series_pass_session_expired_reservation_raises_404(
    online_pass: tuple[SeriesPass, list[TicketTier]], member_user: RevelUser
) -> None:
    series_pass, _ = online_pass
    with patch("stripe.checkout.Session.create"):
        _, reservation_id = SeriesPassPurchaseService(series_pass, member_user).purchase()  # type: ignore[misc]

    Payment.objects.filter(reservation_id=reservation_id).update(expires_at=timezone.now() - timedelta(minutes=1))

    with pytest.raises(HttpError) as exc_info:
        stripe_service.create_series_pass_session(reservation_id=reservation_id)

    assert exc_info.value.status_code == 404


def test_create_series_pass_session_resumes_when_stamped_while_claim_blocked(
    online_pass: tuple[SeriesPass, list[TicketTier]], member_user: RevelUser
) -> None:
    """A double-submit loser (unblocked from the claim after the winner committed its
    stamp) must resume the winner's session instead of re-calling Stripe (#632)."""
    series_pass, _ = online_pass
    with patch("stripe.checkout.Session.create"):
        _, reservation_id = SeriesPassPurchaseService(series_pass, member_user).purchase()  # type: ignore[misc]

    def winner_stamped(rid: UUID) -> None:
        # Simulates the concurrent winner committing while our claim was blocked.
        Payment.objects.filter(reservation_id=rid).update(stripe_session_id="cs_series_winner")

    fake_resume_url = "https://checkout.stripe.com/pay/cs_series_winner"
    with (
        patch.object(stripe_service, "claim_reservation_hold", side_effect=winner_stamped),
        patch("stripe.checkout.Session.create") as mock_create,
        patch.object(stripe_service, "resume_pending_checkout", return_value=fake_resume_url) as mock_resume,
    ):
        url = stripe_service.create_series_pass_session(reservation_id=reservation_id)
        mock_create.assert_not_called()
        mock_resume.assert_called_once()
    assert url == fake_resume_url
