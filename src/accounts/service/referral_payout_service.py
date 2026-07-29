"""Operator-driven recovery for referral payout disbursement (issue #797)."""

import uuid

import structlog
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from accounts.models import ReferralPayout, RevelUser

logger = structlog.get_logger(__name__)

_Status = ReferralPayout.ReferralPayoutStatus


def requeue_failed_payouts(payouts: QuerySet[ReferralPayout], *, actor: RevelUser) -> list[uuid.UUID]:
    """Flip ``FAILED`` payouts back to ``CALCULATED`` so disbursement retries them.

    A transfer can fail for reasons entirely on the platform side (empty platform
    Stripe balance, transient destination issue), and neither
    ``process_referral_payouts`` (scans ``CALCULATED``) nor the stale sweep
    (scans ``PENDING``) ever revisits a ``FAILED`` row — so without this the money
    is stranded. Retrying is safe: the transfer uses the idempotency key
    ``referral-payout-<id>``, so a transfer that actually reached Stripe is
    returned rather than duplicated.

    Rows not in ``FAILED`` are ignored. Selection happens under
    ``select_for_update`` in a stable id order, so a concurrent account-deletion
    cleanup (which locks and deletes unpaid payouts) cannot interleave.

    Args:
        payouts: The payouts to consider.
        actor: The admin performing the requeue, recorded in the audit log.

    Returns:
        The ids of the payouts actually requeued.
    """
    candidate_ids = list(payouts.values_list("id", flat=True))
    with transaction.atomic():
        requeued_ids = list(
            ReferralPayout.objects.select_for_update()
            .filter(id__in=candidate_ids, status=_Status.FAILED)
            .order_by("id")
            .values_list("id", flat=True)
        )
        if requeued_ids:
            ReferralPayout.objects.filter(id__in=requeued_ids).update(
                status=_Status.CALCULATED, updated_at=timezone.now()
            )

    for payout_id in requeued_ids:
        logger.info("payout_requeued", payout_id=str(payout_id), actor_id=str(actor.id), actor_email=actor.email)
    return requeued_ids
