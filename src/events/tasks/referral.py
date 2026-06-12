"""Celery task for monthly referral payout calculation."""

import datetime
import typing as t

import structlog
from celery import shared_task
from django.utils import timezone

if t.TYPE_CHECKING:
    from events.service.referral_payout_service import PayoutResult

logger = structlog.get_logger(__name__)


@shared_task(name="events.calculate_referral_payouts")
def calculate_referral_payouts() -> "PayoutResult":
    """Calculate referral earnings for the previous calendar month.

    Runs on the 1st of each month via Celery beat. For each Referral,
    aggregates platform fees from the referred user's organizations and creates
    a ReferralPayout record. Idempotent — safe to re-run.
    """
    import calendar

    today = timezone.now().date()
    # Previous month
    if today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1

    period_start = datetime.date(year, month, 1)
    period_end = datetime.date(year, month, calendar.monthrange(year, month)[1])

    from events.service.referral_payout_service import calculate_payouts_for_period

    result = calculate_payouts_for_period(period_start, period_end)
    logger.info("referral_payouts_calculated", period=str(period_start), **result)
    return result
