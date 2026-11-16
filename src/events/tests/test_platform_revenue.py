"""Tests for platform-fee totals and membership-fee MRR (#1390)."""

import typing as t
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import (
    MembershipPayment,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    Payment,
    Ticket,
    TicketTier,
)
from events.service import platform_revenue_service as svc

pytestmark = pytest.mark.django_db

START = datetime(2026, 3, 8, 23, 0, tzinfo=UTC)  # Vienna midnight, 9 Mar 2026
END = datetime(2026, 3, 9, 23, 0, tzinfo=UTC)
INSIDE = "2026-03-09 12:00:00"
D = Decimal


@pytest.fixture
def tier(tier_factory: t.Callable[..., TicketTier]) -> TicketTier:
    return tier_factory(name="T")


@pytest.fixture
def make_payment(
    tier: TicketTier,
    ticket_factory: t.Callable[..., Ticket],
    payment_factory: t.Callable[..., Payment],
    django_user_model: type[RevelUser],
) -> t.Callable[..., Payment]:
    counter = {"n": 0}

    def _make(stamp: str = INSIDE, **kwargs: t.Any) -> Payment:
        counter["n"] += 1
        user = django_user_model.objects.create_user(
            username=f"u{counter['n']}", email=f"u{counter['n']}@x.com", password="x"
        )
        with freeze_time(stamp):
            return payment_factory(ticket_factory(user=user, tier=tier), **kwargs)

    return _make


def test_local_midnight_boundaries() -> None:
    now = datetime(2026, 3, 11, 8, 0, tzinfo=UTC)  # Wed
    assert svc.local_midnight(now) == datetime(2026, 3, 10, 23, 0, tzinfo=UTC)
    assert svc.local_midnight(now, days_back=2) == datetime(2026, 3, 8, 23, 0, tzinfo=UTC)  # Monday
    assert svc.local_midnight(now, first_of_month=True) == datetime(2026, 2, 28, 23, 0, tzinfo=UTC)


def test_collected_fees_include_refunded_and_exclude_uncollected(make_payment: t.Callable[..., Payment]) -> None:
    fee: dict[str, t.Any] = {"platform_fee": D("1.20"), "platform_fee_net": D("1.00"), "platform_fee_vat": D("0.20")}
    make_payment(**fee)
    make_payment(status=Payment.PaymentStatus.REFUNDED, **fee)  # collected, then refunded: fee is kept
    make_payment(status=Payment.PaymentStatus.PENDING, **fee)
    make_payment(status=Payment.PaymentStatus.FAILED, **fee)
    make_payment(platform_fee=D("0.00"))  # free ticket
    make_payment("2026-03-08 22:59:59", **fee)  # just before the window
    make_payment("2026-03-09 23:00:00", **fee)  # window end is exclusive

    totals = svc.platform_fee_totals(START, END)

    assert totals == {"EUR": svc.FeeTotals("EUR", gross=D("2.40"), net=D("2.00"), vat=D("0.40"))}


def test_legacy_rows_fall_back_and_are_counted_and_currencies_grouped(make_payment: t.Callable[..., Payment]) -> None:
    make_payment(platform_fee=D("2.00"), platform_fee_net=None, platform_fee_vat=None)
    make_payment(platform_fee=D("1.20"), platform_fee_net=D("1.00"), platform_fee_vat=D("0.20"))
    make_payment(
        platform_fee=D("5.00"), platform_fee_net=D("5.00"), platform_fee_vat=D("0.00"), currency="CHF"
    )  # reverse charge

    totals = svc.platform_fee_totals(START, END)

    assert totals["EUR"] == svc.FeeTotals("EUR", gross=D("3.20"), net=D("3.00"), vat=D("0.20"), legacy_count=1)
    assert totals["CHF"] == svc.FeeTotals("CHF", gross=D("5.00"), net=D("5.00"), vat=D("0.00"))


@pytest.fixture
def plans(organization: Organization) -> tuple[MembershipSubscriptionPlan, MembershipSubscriptionPlan]:
    tier = MembershipTier.objects.create(organization=organization, name="Pro")

    def _plan(name: str, unit: str) -> MembershipSubscriptionPlan:
        return MembershipSubscriptionPlan.objects.create(
            tier=tier, name=name, price=D("10.00"), currency="EUR", period_unit=unit, period_count=1
        )

    return _plan("Monthly", MembershipSubscriptionPlan.PeriodUnit.MONTH), _plan(
        "Yearly", MembershipSubscriptionPlan.PeriodUnit.YEAR
    )


@pytest.fixture
def make_membership_payment(
    organization: Organization,
    plans: tuple[MembershipSubscriptionPlan, MembershipSubscriptionPlan],
    django_user_model: type[RevelUser],
) -> t.Callable[..., MembershipPayment]:
    counter = {"n": 0}

    def _make(
        days: int = 30,
        sub_status: str = MembershipSubscription.SubscriptionStatus.ACTIVE,
        sub: MembershipSubscription | None = None,
        period_end: datetime = END,
        **kwargs: t.Any,
    ) -> MembershipPayment:
        if sub is None:
            counter["n"] += 1
            user = django_user_model.objects.create_user(
                username=f"m{counter['n']}", email=f"m{counter['n']}@x.com", password="x"
            )
            sub = MembershipSubscription.objects.create(
                user=user, plan=plans[0], organization=organization, status=sub_status
            )
        defaults: dict[str, t.Any] = {
            "subscription": sub,
            "currency": "EUR",
            "amount": D("10.00"),
            "platform_fee": D("1.20"),
            "platform_fee_net": D("1.00"),
            "platform_fee_vat": D("0.20"),
            "period_start": period_end - timedelta(days=days),
            "period_end": period_end,
            "occurred_at": datetime(2026, 3, 9, 12, tzinfo=UTC),
        }
        defaults.update(kwargs)
        return MembershipPayment.objects.create(**defaults)

    return _make


def test_membership_fees_collected_rules(make_membership_payment: t.Callable[..., MembershipPayment]) -> None:
    make_membership_payment()
    make_membership_payment(status=MembershipPayment.PaymentStatus.REFUNDED)
    make_membership_payment(status=MembershipPayment.PaymentStatus.VOIDED)
    make_membership_payment(status=MembershipPayment.PaymentStatus.FAILED)
    make_membership_payment(platform_fee=D("0.00"), platform_fee_net=None, platform_fee_vat=None)  # offline
    make_membership_payment(occurred_at=datetime(2026, 3, 8, 22, 59, tzinfo=UTC))  # occurred before the window

    assert svc.platform_fee_totals(START, END) == {
        "EUR": svc.FeeTotals("EUR", gross=D("2.40"), net=D("2.00"), vat=D("0.40"))
    }


def test_mrr_normalises_periods_uses_latest_payment_and_live_statuses(
    make_membership_payment: t.Callable[..., MembershipPayment],
) -> None:
    make_membership_payment(days=30)  # monthly: net 1.00 -> 1.00
    make_membership_payment(days=365)  # yearly: net 1.00 per year -> 30/365
    make_membership_payment(sub_status=MembershipSubscription.SubscriptionStatus.PAST_DUE, days=30)
    make_membership_payment(sub_status=MembershipSubscription.SubscriptionStatus.CANCELLED)
    make_membership_payment(sub_status=MembershipSubscription.SubscriptionStatus.EXPIRED)
    make_membership_payment(sub_status=MembershipSubscription.SubscriptionStatus.PENDING)
    make_membership_payment(platform_fee=D("0.00"), platform_fee_net=None, platform_fee_vat=None)  # free/offline
    # Only the most recent fee-bearing payment of a subscription counts.
    renewed = make_membership_payment(
        days=30, platform_fee=D("2.40"), platform_fee_net=D("2.00"), platform_fee_vat=D("0.40")
    )
    make_membership_payment(sub=renewed.subscription, days=30, period_end=END - timedelta(days=60))

    mrr = svc.membership_fee_mrr()

    # monthly 1.00 + yearly 0.0822 + past_due 1.00 + renewed 2.00
    assert mrr["EUR"].net == D("4.08")
    assert mrr["EUR"].vat == D("0.82")
    assert mrr["EUR"].gross == D("4.90")
