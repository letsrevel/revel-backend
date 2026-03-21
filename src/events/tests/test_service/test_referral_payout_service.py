# src/events/tests/test_service/test_referral_payout_service.py
"""Tests for the referral payout calculation service."""

import datetime
import typing as t
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import Referral, ReferralCode, ReferralPayout, RevelUser
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service.referral_payout_service import calculate_payouts_for_period

pytestmark = pytest.mark.django_db

PERIOD_START = datetime.date(2026, 2, 1)
PERIOD_END = datetime.date(2026, 2, 28)


@pytest.fixture
def referrer(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="referrer@example.com", email="referrer@example.com", password="pass"
    )


@pytest.fixture
def referred_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="referred@example.com", email="referred@example.com", password="pass"
    )


@pytest.fixture
def buyer(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="buyer@example.com", email="buyer@example.com", password="pass"
    )


@pytest.fixture
def referral(referrer: RevelUser, referred_user: RevelUser) -> Referral:
    code = ReferralCode.objects.create(user=referrer, code="REF100")
    return Referral.objects.create(
        referral_code=code,
        referred_user=referred_user,
        revenue_share_percent=Decimal("15.00"),
    )


@pytest.fixture
def organization(referred_user: RevelUser) -> Organization:
    return Organization.objects.create(name="Referred Org", owner=referred_user)


@pytest.fixture
def event(organization: Organization) -> Event:
    now = timezone.now()
    return Event.objects.create(
        organization=organization,
        name="Test Event",
        starts_at=now,
        ends_at=now + datetime.timedelta(hours=2),
    )


@pytest.fixture
def tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="General",
        price=Decimal("50.00"),
        quantity_total=100,
    )


def _create_payment(
    tier: TicketTier,
    buyer: RevelUser,
    platform_fee: Decimal,
    status: str = Payment.PaymentStatus.SUCCEEDED,
    created_at: datetime.datetime | None = None,
) -> Payment:
    ticket = Ticket.objects.create(
        event=tier.event,
        tier=tier,
        user=buyer,
        status=Ticket.TicketStatus.CONFIRMED,
    )
    payment = Payment.objects.create(
        ticket=ticket,
        user=buyer,
        stripe_session_id=f"sess_{ticket.id}",
        status=status,
        amount=tier.price,
        platform_fee=platform_fee,
    )
    if created_at:
        Payment.objects.filter(pk=payment.pk).update(created_at=created_at)
    return payment


def test_no_referrals(self: None = None) -> None:
    """Test with no referrals returns zeros."""
    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)
    assert result == {"created": 0, "skipped": 0}


def test_referral_no_org(referral: Referral) -> None:
    """Test referral where referred user has no organizations is skipped."""
    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)
    assert result == {"created": 0, "skipped": 1}


def test_referral_no_payments(referral: Referral, organization: Organization) -> None:
    """Test referral with org but no payments is skipped."""
    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)
    assert result == {"created": 0, "skipped": 1}


def test_payout_created(
    referral: Referral, tier: TicketTier, buyer: RevelUser
) -> None:
    """Test that a payout is correctly calculated from platform fees."""
    _create_payment(
        tier, buyer, platform_fee=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )
    _create_payment(
        tier, buyer, platform_fee=Decimal("20.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 20, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 1, "skipped": 0}
    payout = ReferralPayout.objects.get(referral=referral)
    assert payout.gross_platform_fees == Decimal("30.00")
    # 30.00 * 15% = 4.50
    assert payout.payout_amount == Decimal("4.50")
    assert payout.status == ReferralPayout.Status.CALCULATED
    assert payout.period_start == PERIOD_START
    assert payout.period_end == PERIOD_END


def test_payments_outside_period_excluded(
    referral: Referral, tier: TicketTier, buyer: RevelUser
) -> None:
    """Test that payments outside the period are not counted."""
    # Inside period
    _create_payment(
        tier, buyer, platform_fee=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )
    # Outside period (March)
    _create_payment(
        tier, buyer, platform_fee=Decimal("50.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 3, 5, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 1, "skipped": 0}
    payout = ReferralPayout.objects.get(referral=referral)
    assert payout.gross_platform_fees == Decimal("10.00")


def test_failed_payments_excluded(
    referral: Referral, tier: TicketTier, buyer: RevelUser
) -> None:
    """Test that non-succeeded payments are not counted."""
    _create_payment(
        tier, buyer, platform_fee=Decimal("10.00"),
        status=Payment.PaymentStatus.FAILED,
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 0, "skipped": 1}
    assert not ReferralPayout.objects.exists()


def test_idempotent(
    referral: Referral, tier: TicketTier, buyer: RevelUser
) -> None:
    """Test that re-running for the same period does not create duplicates."""
    _create_payment(
        tier, buyer, platform_fee=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    result1 = calculate_payouts_for_period(PERIOD_START, PERIOD_END)
    result2 = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result1 == {"created": 1, "skipped": 0}
    assert result2 == {"created": 0, "skipped": 1}
    assert ReferralPayout.objects.count() == 1
