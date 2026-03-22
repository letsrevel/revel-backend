"""Service for calculating monthly referral payouts.

This service lives in the events app because it queries Payment and Organization
models to compute payout amounts. The ReferralPayout model itself lives in
accounts (the referral domain owner). See ADR in docs/ for the rationale.
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal

import structlog
from django.db.models import Sum

from accounts.models import Referral, ReferralPayout
from events.models import Organization, Payment

logger = structlog.get_logger(__name__)


def calculate_payouts_for_period(period_start: datetime.date, period_end: datetime.date) -> dict[str, int]:
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

    for referral in Referral.objects.select_related("referred_user").iterator():
        referred_orgs = Organization.objects.filter(owner=referral.referred_user)
        if not referred_orgs.exists():
            skipped += 1
            continue

        gross = Payment.objects.filter(
            ticket__event__organization__in=referred_orgs,
            status=Payment.PaymentStatus.SUCCEEDED,
            created_at__date__gte=period_start,
            created_at__date__lte=period_end,
        ).aggregate(total=Sum("platform_fee"))["total"] or Decimal("0")

        if gross == 0:
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

    return {"created": created, "skipped": skipped}
