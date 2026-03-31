"""Service for calculating monthly referral payouts.

This service lives in the events app because it queries Payment and Organization
models to compute payout amounts. The ReferralPayout model itself lives in
accounts (the referral domain owner). See ADR-0009 for the rationale.
"""

import datetime
import typing as t
from decimal import ROUND_HALF_UP, Decimal

import structlog
from django.conf import settings
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import Referral, ReferralPayout
from common.service.exchange_rate_service import convert_using_rates, get_latest_rates
from events.models import Payment

logger = structlog.get_logger(__name__)


class PayoutResult(t.TypedDict):
    created: int
    skipped: int


def _calculate_net_fees(
    referral: Referral,
    period_start_dt: datetime.datetime,
    period_end_dt: datetime.datetime,
    rates: dict[str, float],
    platform_currency: str,
) -> Decimal:
    """Aggregate net platform fees for a referral, converting all currencies to platform currency."""
    fee_by_currency = (
        Payment.objects.filter(
            ticket__event__organization__owner=referral.referred_user,
            status=Payment.PaymentStatus.SUCCEEDED,
            created_at__gte=period_start_dt,
            created_at__lt=period_end_dt,
        )
        .values("currency")
        .annotate(total=Sum(Coalesce("platform_fee_net", "platform_fee")))
    )

    net_fees = Decimal("0")
    for entry in fee_by_currency:
        amount = entry["total"] or Decimal("0")
        if amount:
            net_fees += convert_using_rates(amount, entry["currency"], platform_currency, rates)

    return net_fees


def _create_payout_record(
    referral: Referral,
    period_start: datetime.date,
    period_end: datetime.date,
    net_fees: Decimal,
    platform_currency: str,
) -> bool:
    """Create a ReferralPayout record with rollover from prior periods.

    Returns:
        True if a new record was created, False if it already existed.
    """
    current_period_share = (net_fees * referral.revenue_share_percent / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    prior_payouts_qs = ReferralPayout.objects.filter(
        referral=referral,
        status=ReferralPayout.ReferralPayoutStatus.CALCULATED,
        period_start__lt=period_start,
    )
    rolled_over: Decimal = prior_payouts_qs.aggregate(total=Sum("payout_amount"))["total"] or Decimal("0")

    payout_amount = current_period_share + rolled_over

    _, was_created = ReferralPayout.objects.get_or_create(
        referral=referral,
        period_start=period_start,
        defaults={
            "period_end": period_end,
            "net_platform_fees": net_fees,
            "payout_amount": payout_amount,
            "rolled_over_amount": rolled_over,
            "currency": platform_currency,
            "status": ReferralPayout.ReferralPayoutStatus.CALCULATED,
        },
    )

    if was_created:
        if rolled_over:
            rolled_count = prior_payouts_qs.update(status=ReferralPayout.ReferralPayoutStatus.ROLLED_OVER)
            logger.info(
                "prior_payouts_rolled_over",
                referral_id=str(referral.id),
                rolled_over_count=rolled_count,
                rolled_over_amount=str(rolled_over),
            )

        logger.info(
            "referral_payout_created",
            referral_id=str(referral.id),
            referrer_id=str(referral.referrer_id),
            period=str(period_start),
            net_fees=str(net_fees),
            payout=str(payout_amount),
            rolled_over=str(rolled_over),
            currency=platform_currency,
        )

    return was_created


def calculate_payouts_for_period(period_start: datetime.date, period_end: datetime.date) -> PayoutResult:
    """Calculate referral payouts for a given period.

    For each Referral, aggregates net platform fees (excluding VAT) from
    succeeded payments on events owned by the referred user's organizations,
    converts all amounts to DEFAULT_CURRENCY, then creates a ReferralPayout
    record with the referrer's revenue share applied.

    Falls back to gross platform_fee for historical payments where
    platform_fee_net is null.

    Uses get_or_create to ensure idempotency (safe to re-run).

    Args:
        period_start: First day of the payout period (inclusive).
        period_end: Last day of the payout period (inclusive).

    Returns:
        A dict with counts: {"created": N, "skipped": N}.
    """
    created = 0
    skipped = 0

    platform_currency: str = settings.DEFAULT_CURRENCY

    period_start_dt = timezone.make_aware(datetime.datetime.combine(period_start, datetime.time.min))
    period_end_dt = timezone.make_aware(
        datetime.datetime.combine(period_end + datetime.timedelta(days=1), datetime.time.min)
    )

    exchange_rate = get_latest_rates()
    rates = exchange_rate.rates

    for referral in Referral.objects.select_related("referred_user").iterator():
        net_fees = _calculate_net_fees(referral, period_start_dt, period_end_dt, rates, platform_currency)

        if not net_fees:
            logger.debug(
                "referral_payout_skipped_zero_revenue",
                referral_id=str(referral.id),
                period=str(period_start),
            )
            skipped += 1
            continue

        was_created = _create_payout_record(referral, period_start, period_end, net_fees, platform_currency)
        if was_created:
            created += 1
        else:
            skipped += 1

    return PayoutResult(created=created, skipped=skipped)
