# src/events/tests/test_service/test_referral_payout_service.py
"""Tests for the referral payout calculation service."""

import datetime
import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.conf import settings
from django.utils import timezone

from accounts.models import Referral, ReferralCode, ReferralPayout, RevelUser
from common.models import ExchangeRate
from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service.referral_payout_service import calculate_payouts_for_period
from events.tasks import calculate_referral_payouts

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
        start=now,
        end=now + datetime.timedelta(hours=2),
    )


@pytest.fixture
def tier(event: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=event,
        name="General",
        price=Decimal("50.00"),
        total_quantity=100,
    )


def _create_payment(
    tier: TicketTier,
    buyer: RevelUser,
    platform_fee: Decimal,
    platform_fee_net: Decimal | None = None,
    status: str = Payment.PaymentStatus.SUCCEEDED,
    created_at: datetime.datetime | None = None,
    currency: str | None = None,
) -> Payment:
    ticket = Ticket.objects.create(
        event=tier.event,
        tier=tier,
        user=buyer,
        guest_name=buyer.username,
        status=Ticket.TicketStatus.ACTIVE,
    )
    payment = Payment.objects.create(
        ticket=ticket,
        user=buyer,
        stripe_session_id=f"sess_{ticket.id}",
        status=status,
        amount=tier.price,
        platform_fee=platform_fee,
        platform_fee_net=platform_fee_net,
        currency=currency or tier.currency,
    )
    if created_at:
        Payment.objects.filter(pk=payment.pk).update(created_at=created_at)
    return payment


def test_no_referrals() -> None:
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


def test_payout_created(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """Test that a payout is correctly calculated from net platform fees."""
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("12.00"),
        platform_fee_net=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("24.00"),
        platform_fee_net=Decimal("20.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 20, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 1, "skipped": 0}
    payout = ReferralPayout.objects.get(referral=referral)
    # Uses net fees (10 + 20 = 30), not gross (12 + 24 = 36)
    assert payout.net_platform_fees == Decimal("30.00")
    # 30.00 * 15% = 4.50
    assert payout.payout_amount == Decimal("4.50")
    assert payout.status == ReferralPayout.Status.CALCULATED
    assert payout.period_start == PERIOD_START
    assert payout.period_end == PERIOD_END
    assert payout.currency == settings.DEFAULT_CURRENCY


def test_payments_outside_period_excluded(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """Test that payments outside the period are not counted."""
    # Inside period
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )
    # Outside period (March)
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("50.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 3, 5, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 1, "skipped": 0}
    payout = ReferralPayout.objects.get(referral=referral)
    assert payout.net_platform_fees == Decimal("10.00")


def test_failed_payments_excluded(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """Test that non-succeeded payments are not counted."""
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("10.00"),
        status=Payment.PaymentStatus.FAILED,
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 0, "skipped": 1}
    assert not ReferralPayout.objects.exists()


def test_falls_back_to_gross_when_net_is_null(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """Test that historical payments without platform_fee_net fall back to platform_fee."""
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("10.00"),
        platform_fee_net=None,
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 1, "skipped": 0}
    payout = ReferralPayout.objects.get(referral=referral)
    assert payout.net_platform_fees == Decimal("10.00")
    # 10.00 * 15% = 1.50
    assert payout.payout_amount == Decimal("1.50")


def test_zero_net_fees_skipped(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """Test that a payment with explicitly zero platform_fee_net is skipped."""
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("0.00"),
        platform_fee_net=Decimal("0.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 0, "skipped": 1}
    assert not ReferralPayout.objects.exists()


def test_multiple_organizations_aggregated(referral: Referral, referred_user: RevelUser, buyer: RevelUser) -> None:
    """Test that fees from multiple orgs owned by the referred user are aggregated."""
    org1 = Organization.objects.create(name="Org One", owner=referred_user)
    org2 = Organization.objects.create(name="Org Two", owner=referred_user)

    now = timezone.now()
    event1 = Event.objects.create(organization=org1, name="Event 1", start=now, end=now + datetime.timedelta(hours=2))
    event2 = Event.objects.create(organization=org2, name="Event 2", start=now, end=now + datetime.timedelta(hours=2))

    tier1 = TicketTier.objects.create(event=event1, name="T1", price=Decimal("50.00"), total_quantity=100)
    tier2 = TicketTier.objects.create(event=event2, name="T2", price=Decimal("50.00"), total_quantity=100)

    _create_payment(
        tier1,
        buyer,
        platform_fee=Decimal("12.00"),
        platform_fee_net=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 10, 12, 0)),
    )
    _create_payment(
        tier2,
        buyer,
        platform_fee=Decimal("24.00"),
        platform_fee_net=Decimal("20.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 20, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 1, "skipped": 0}
    payout = ReferralPayout.objects.get(referral=referral)
    # 10 + 20 = 30 net fees across two orgs
    assert payout.net_platform_fees == Decimal("30.00")
    # 30.00 * 15% = 4.50
    assert payout.payout_amount == Decimal("4.50")


def test_idempotent(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """Test that re-running for the same period does not create duplicates."""
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    result1 = calculate_payouts_for_period(PERIOD_START, PERIOD_END)
    result2 = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result1 == {"created": 1, "skipped": 0}
    assert result2 == {"created": 0, "skipped": 1}
    assert ReferralPayout.objects.count() == 1


def test_multiple_referrals(
    referral: Referral,
    tier: TicketTier,
    buyer: RevelUser,
    django_user_model: t.Type[RevelUser],
) -> None:
    """Test that multiple referrals are processed in a single run."""
    # First referral has payments
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    # Second referral with no org (will be skipped)
    referrer2 = django_user_model.objects.create_user(
        username="referrer2@example.com", email="referrer2@example.com", password="pass"
    )
    referred2 = django_user_model.objects.create_user(
        username="referred2@example.com", email="referred2@example.com", password="pass"
    )
    code2 = ReferralCode.objects.create(user=referrer2, code="REF200")
    Referral.objects.create(
        referral_code=code2,
        referred_user=referred2,
        revenue_share_percent=Decimal("10.00"),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 1, "skipped": 1}


@patch("django.utils.timezone.now")
def test_task_date_arithmetic_march(mock_now: t.Any) -> None:
    """Test that the task computes the correct previous month (Feb from March)."""
    mock_now.return_value = timezone.make_aware(datetime.datetime(2026, 3, 1, 6, 0))

    result = calculate_referral_payouts()

    assert result == {"created": 0, "skipped": 0}
    # Verify no payouts created (no referrals), but the task ran without error


@patch("django.utils.timezone.now")
def test_task_date_arithmetic_january_rollover(mock_now: t.Any) -> None:
    """Test that the task correctly rolls back to December when run in January."""
    mock_now.return_value = timezone.make_aware(datetime.datetime(2026, 1, 1, 6, 0))

    with patch(
        "events.service.referral_payout_service.calculate_payouts_for_period",
        wraps=calculate_payouts_for_period,
    ) as mock_calc:
        calculate_referral_payouts()
        mock_calc.assert_called_once_with(
            datetime.date(2025, 12, 1),
            datetime.date(2025, 12, 31),
        )


# ---------------------------------------------------------------------------
# Rollover tests
# ---------------------------------------------------------------------------


def test_rollover_accumulates_prior_calculated_payouts(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """Prior CALCULATED payouts are rolled into the new period's payout_amount."""
    # Create a small payout for January (below €5 threshold)
    ReferralPayout.objects.create(
        referral=referral,
        period_start=datetime.date(2026, 1, 1),
        period_end=datetime.date(2026, 1, 31),
        net_platform_fees=Decimal("10.00"),
        payout_amount=Decimal("1.50"),
        currency=settings.DEFAULT_CURRENCY,
        status=ReferralPayout.Status.CALCULATED,
    )

    # February has revenue
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("24.00"),
        platform_fee_net=Decimal("20.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 1, "skipped": 0}
    feb_payout = ReferralPayout.objects.get(referral=referral, period_start=PERIOD_START)
    # 20.00 * 15% = 3.00 current + 1.50 rolled over = 4.50
    assert feb_payout.payout_amount == Decimal("4.50")
    assert feb_payout.rolled_over_amount == Decimal("1.50")
    assert feb_payout.net_platform_fees == Decimal("20.00")  # only current period fees

    # Prior payout is now ROLLED_OVER
    jan_payout = ReferralPayout.objects.get(referral=referral, period_start=datetime.date(2026, 1, 1))
    assert jan_payout.status == ReferralPayout.Status.ROLLED_OVER


def test_rollover_multiple_prior_periods(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """Multiple prior CALCULATED payouts are all rolled into the new one."""
    for month in (1, 2):
        start = datetime.date(2025, month, 1)
        ReferralPayout.objects.create(
            referral=referral,
            period_start=start,
            period_end=datetime.date(2025, month, 28),
            net_platform_fees=Decimal("10.00"),
            payout_amount=Decimal("1.50"),
            currency=settings.DEFAULT_CURRENCY,
            status=ReferralPayout.Status.CALCULATED,
        )

    # March 2025 has revenue
    march_start = datetime.date(2025, 3, 1)
    march_end = datetime.date(2025, 3, 31)
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("12.00"),
        platform_fee_net=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2025, 3, 15, 12, 0)),
    )

    result = calculate_payouts_for_period(march_start, march_end)

    assert result == {"created": 1, "skipped": 0}
    payout = ReferralPayout.objects.get(referral=referral, period_start=march_start)
    # 10.00 * 15% = 1.50 current + 1.50 + 1.50 rolled = 4.50
    assert payout.payout_amount == Decimal("4.50")
    assert payout.rolled_over_amount == Decimal("3.00")

    # Both prior payouts are ROLLED_OVER
    assert ReferralPayout.objects.filter(status=ReferralPayout.Status.ROLLED_OVER).count() == 2


def test_no_rollover_when_no_prior_calculated(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """When there are no prior CALCULATED payouts, rolled_over_amount is 0."""
    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("12.00"),
        platform_fee_net=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    payout = ReferralPayout.objects.get(referral=referral)
    assert payout.rolled_over_amount == Decimal("0.00")
    assert payout.payout_amount == Decimal("1.50")


def test_paid_payouts_not_rolled_over(referral: Referral, tier: TicketTier, buyer: RevelUser) -> None:
    """Already-paid payouts are not included in rollover."""
    ReferralPayout.objects.create(
        referral=referral,
        period_start=datetime.date(2026, 1, 1),
        period_end=datetime.date(2026, 1, 31),
        net_platform_fees=Decimal("100.00"),
        payout_amount=Decimal("15.00"),
        currency=settings.DEFAULT_CURRENCY,
        status=ReferralPayout.Status.PAID,
        stripe_transfer_id="tr_already_paid",
    )

    _create_payment(
        tier,
        buyer,
        platform_fee=Decimal("12.00"),
        platform_fee_net=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )

    calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    payout = ReferralPayout.objects.get(referral=referral, period_start=PERIOD_START)
    assert payout.rolled_over_amount == Decimal("0.00")
    assert payout.payout_amount == Decimal("1.50")

    # Paid payout unchanged
    jan = ReferralPayout.objects.get(period_start=datetime.date(2026, 1, 1))
    assert jan.status == ReferralPayout.Status.PAID


def test_multi_currency_converted_to_platform_currency(
    referral: Referral,
    referred_user: RevelUser,
    buyer: RevelUser,
) -> None:
    """Test that payments in different currencies are converted to DEFAULT_CURRENCY."""
    # Clear seed data from migration so we control the exact rates
    ExchangeRate.objects.all().delete()
    ExchangeRate.objects.create(
        base="EUR",
        date=PERIOD_END,
        rates={"USD": 1.08, "GBP": 0.86},
    )

    org = Organization.objects.create(name="Multi-Currency Org", owner=referred_user)
    now = timezone.now()

    # EUR event
    event_eur = Event.objects.create(
        organization=org, name="EUR Event", start=now, end=now + datetime.timedelta(hours=2)
    )
    tier_eur = TicketTier.objects.create(event=event_eur, name="EUR", price=Decimal("50.00"))

    # USD event
    event_usd = Event.objects.create(
        organization=org, name="USD Event", start=now, end=now + datetime.timedelta(hours=2)
    )
    tier_usd = TicketTier.objects.create(event=event_usd, name="USD", price=Decimal("50.00"), currency="USD")

    # EUR payment: €10 net
    _create_payment(
        tier_eur,
        buyer,
        platform_fee=Decimal("12.00"),
        platform_fee_net=Decimal("10.00"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 15, 12, 0)),
    )
    # USD payment: $10.80 net → should convert to €10.00
    _create_payment(
        tier_usd,
        buyer,
        platform_fee=Decimal("12.96"),
        platform_fee_net=Decimal("10.80"),
        created_at=timezone.make_aware(datetime.datetime(2026, 2, 20, 12, 0)),
    )

    result = calculate_payouts_for_period(PERIOD_START, PERIOD_END)

    assert result == {"created": 1, "skipped": 0}
    payout = ReferralPayout.objects.get(referral=referral)
    # €10.00 (EUR) + $10.80 / 1.08 = €10.00 → total €20.00
    assert payout.net_platform_fees == Decimal("20.00")
    assert payout.currency == "EUR"
    # 20.00 * 15% = 3.00
    assert payout.payout_amount == Decimal("3.00")
