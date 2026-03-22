"""Service for calculating monthly referral payouts.

This service lives in the events app because it queries Payment and Organization
models to compute payout amounts. The ReferralPayout model itself lives in
accounts (the referral domain owner). See ADR-0009 for the rationale.
"""

import datetime
import typing as t
from decimal import ROUND_HALF_UP, Decimal

import structlog
from django.db.models import Sum
from django.utils import timezone

from accounts.models import Referral, ReferralPayout
from events.models import Payment

logger = structlog.get_logger(__name__)


class PayoutResult(t.TypedDict):
    created: int
    skipped: int


def calculate_payouts_for_period(period_start: datetime.date, period_end: datetime.date) -> PayoutResult:
    """Calculate referral payouts for a given period.

    For each active Referral, aggregates platform fees from succeeded payments
    on events owned by the referred user's organizations, then creates a
    ReferralPayout record with the referrer's revenue share applied.

    Uses get_or_create to ensure idempotency (safe to re-run).

    Args:
        period_start: First day of the payout period (inclusive).
        period_end: Last day of the payout period (inclusive).

    Returns:
        A dict with counts: {"created": N, "skipped": N}.
    """
    created = 0
    skipped = 0

    # Use timezone-aware datetime boundaries so the created_at index is used
    # (created_at__date__gte forces a DATE() cast in SQL, bypassing the index)
    period_start_dt = timezone.make_aware(datetime.datetime.combine(period_start, datetime.time.min))
    period_end_dt = timezone.make_aware(datetime.datetime.combine(period_end + datetime.timedelta(days=1), datetime.time.min))

    for referral in Referral.objects.select_related("referred_user").iterator():
        gross = Payment.objects.filter(
            ticket__event__organization__owner=referral.referred_user,
            status=Payment.PaymentStatus.SUCCEEDED,
            created_at__gte=period_start_dt,
            created_at__lt=period_end_dt,
        ).aggregate(total=Sum("platform_fee"))["total"] or Decimal("0")

        if not gross:
            skipped += 1
            continue

        payout_amount = (gross * referral.revenue_share_percent / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        _, was_created = ReferralPayout.objects.get_or_create(
            referral=referral,
            period_start=period_start,
            defaults={
                "period_end": period_end,
                "gross_platform_fees": gross,
                "payout_amount": payout_amount,
                "status": ReferralPayout.Status.CALCULATED,
            },
        )

        if was_created:
            created += 1
            logger.info(
                "referral_payout_created",
                referral_id=str(referral.id),
                referrer_id=str(referral.referrer_id),
                period=str(period_start),
                gross=str(gross),
                payout=str(payout_amount),
            )
        else:
            skipped += 1

    return PayoutResult(created=created, skipped=skipped)
