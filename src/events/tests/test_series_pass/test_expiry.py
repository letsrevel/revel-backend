"""Tests for pass-aware expiry of abandoned online series-pass checkouts.

Three expiry routes share ``expire_stranded_held_passes``: the
``cleanup_expired_payments`` beat task, the resume/cancel-checkout batch cleanup
(``_cleanup_expired_batch`` / ``cancel_pending_checkout``), and the
``payment_intent.canceled`` webhook. Each must flip the stranded PENDING pass to
CANCELLED, restore tier + pass counters, and let the buyer purchase again.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest
import stripe
from django.utils import timezone

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
from events.service.stripe_webhooks import StripeEventHandler
from events.tasks.payments import cleanup_expired_payments

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
def online_pass_two_tiers(
    stripe_connected_organization: Organization, event_series: EventSeries
) -> tuple[SeriesPass, list[TicketTier]]:
    """An ONLINE pass covering two future events, one tier each."""
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Expiry Pass",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )
    now = timezone.now()
    tiers = []
    for i in range(2):
        event = Event.objects.create(
            organization=stripe_connected_organization,
            name=f"Expiry Event {i}",
            slug=f"expiry-event-{i}",
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
            name=f"Expiry Tier {i}",
            price=Decimal("10.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.ONLINE,
        )
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
        tiers.append(tier)
    return series_pass, tiers


def _purchase(series_pass: SeriesPass, user: RevelUser, session_id: str) -> HeldSeriesPass:
    """Drive the real ONLINE purchase flow (mocked Stripe) to a PENDING HeldSeriesPass."""
    mock_session = Mock()
    mock_session.id = session_id
    mock_session.url = f"https://checkout.stripe.com/pay/{session_id}"
    with patch("stripe.checkout.Session.create", return_value=mock_session):
        SeriesPassPurchaseService(series_pass, user).purchase()
    return HeldSeriesPass.objects.get(series_pass=series_pass, user=user, status=HeldSeriesPass.Status.PENDING)


class TestBeatTaskExpiry:
    def test_expired_checkout_cancels_pass_restores_counters_and_allows_repurchase(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_beat")

        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 1
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 1

        Payment.objects.filter(stripe_session_id="cs_expiry_beat").update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        cleaned = cleanup_expired_payments()
        assert cleaned == 2

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        assert not Ticket.objects.filter(held_pass=held_pass).exists()

        # The buyer is no longer blocked by the conditional unique constraint.
        new_pass = _purchase(series_pass, revel_user, "cs_expiry_retry")
        assert new_pass.pk != held_pass.pk
        assert new_pass.status == HeldSeriesPass.Status.PENDING

    def test_beat_task_leaves_unexpired_pass_checkouts_alone(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, _ = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_fresh")

        cleaned = cleanup_expired_payments()
        assert cleaned == 0

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.PENDING


class TestCleanupExpiredBatch:
    def test_multi_tier_batch_decrements_each_tier_not_one(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        """A pass batch spans N tiers; the old code decremented ONE tier by N."""
        series_pass, tiers = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_batch")

        payment = Payment.objects.filter(stripe_session_id="cs_expiry_batch").select_related("ticket__tier").first()
        assert payment is not None

        stripe_service._cleanup_expired_batch(payment)

        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        assert not Payment.objects.filter(stripe_session_id="cs_expiry_batch").exists()

    def test_cancel_pending_checkout_releases_pass_and_all_tiers(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_user_cancel")
        payment = Payment.objects.filter(stripe_session_id="cs_expiry_user_cancel").first()
        assert payment is not None

        cancelled = stripe_service.cancel_pending_checkout(str(payment.id), revel_user)
        assert cancelled == 2

        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        assert not Ticket.objects.filter(held_pass=held_pass).exists()


def _canceled_intent_event(payment_intent_id: str) -> MagicMock:
    """Build a fake, iterable ``payment_intent.canceled`` stripe.Event."""
    intent_data = {"id": payment_intent_id}
    event_data = {"type": "payment_intent.canceled", "data": {"object": intent_data}}
    mock_event = MagicMock(spec=stripe.Event)
    mock_event.__iter__.return_value = iter(event_data.items())
    mock_event.type = event_data["type"]
    mock_event.data = MagicMock()
    mock_event.data.object = intent_data
    return mock_event


class TestPaymentIntentCanceledWebhook:
    def test_canceled_intent_cancels_pass_and_restores_pass_counter(
        self,
        online_pass_two_tiers: tuple[SeriesPass, list[TicketTier]],
        revel_user: RevelUser,
    ) -> None:
        series_pass, tiers = online_pass_two_tiers
        held_pass = _purchase(series_pass, revel_user, "cs_expiry_intent")
        Payment.objects.filter(stripe_session_id="cs_expiry_intent").update(stripe_payment_intent_id="pi_expiry_intent")

        event = _canceled_intent_event("pi_expiry_intent")
        StripeEventHandler(event).handle_payment_intent_canceled(event)

        held_pass.refresh_from_db()
        assert held_pass.status == HeldSeriesPass.Status.CANCELLED
        series_pass.refresh_from_db()
        assert series_pass.quantity_sold == 0
        for tier in tiers:
            tier.refresh_from_db()
            assert tier.quantity_sold == 0
        for payment in Payment.objects.filter(stripe_session_id="cs_expiry_intent"):
            assert payment.status == Payment.PaymentStatus.FAILED
        for ticket in Ticket.objects.filter(held_pass=held_pass):
            assert ticket.status == Ticket.TicketStatus.CANCELLED
