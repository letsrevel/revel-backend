"""Task for processing referral payouts via Stripe Transfer."""

from datetime import timedelta

import stripe
import structlog
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import ReferralPayout, ReferralPayoutStatement, RevelUser
from common.models import SiteSettings
from common.tasks import send_email
from events.utils.currency import to_stripe_amount

logger = structlog.get_logger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY
stripe.api_version = settings.STRIPE_API_VERSION


def _reclaim_stale_payouts() -> None:
    """Reclaim PENDING payouts abandoned by crashed workers (stale > 1 hour)."""
    stale_cutoff = timezone.now() - timedelta(hours=1)
    stale_count = ReferralPayout.objects.filter(
        status=ReferralPayout.ReferralPayoutStatus.PENDING,
        updated_at__lt=stale_cutoff,
    ).update(status=ReferralPayout.ReferralPayoutStatus.CALCULATED)
    if stale_count:
        logger.warning("stale_pending_payouts_reclaimed", count=stale_count)


def _dispatch_statement(payout_id: str) -> None:
    """Dispatch the retryable statement+email task, isolating broker failures.

    A dispatch failure (e.g. broker outage) must never halt the caller — the
    payout is already PAID, and the backstop sweep re-dispatches on the next run.
    """
    try:
        generate_and_send_payout_statement.delay(payout_id)
    except Exception as exc:  # broker unavailable — isolate per the function contract
        logger.error("payout_statement_dispatch_failed", payout_id=payout_id, error=str(exc))


def _redispatch_missing_statements() -> None:
    """Backstop: re-dispatch statement+email for PAID payouts that never got one.

    Covers the rare case where the in-loop dispatch failed (e.g. broker outage)
    or the retryable task exhausted its retries, leaving a PAID payout without a
    statement — or with a statement whose email delivery never succeeded
    (``email_sent_at`` still null) — which reruns never revisit (they only scan
    ``CALCULATED``). Without the delivery check, a send that exhausted its retries
    would be silently dropped forever (issue #616).

    Idempotent: ``generate_payout_statement`` returns any existing statement and
    ``mark_email_sent`` is a no-op once set, so at worst a recipient gets a
    duplicate email (at-least-once), never zero.
    """
    missing_ids = list(
        ReferralPayout.objects.filter(status=ReferralPayout.ReferralPayoutStatus.PAID)
        .filter(Q(statement__isnull=True) | Q(statement__email_sent_at__isnull=True))
        .values_list("id", flat=True)
    )
    if not missing_ids:
        return
    for payout_id in missing_ids:
        _dispatch_statement(str(payout_id))
    logger.warning("payout_statements_redispatched", count=len(missing_ids))


def _validate_payout_eligibility(payout: ReferralPayout, referrer: RevelUser) -> str | None:
    """Run pre-flight checks for a payout. Returns a skip reason or None if eligible."""
    if not referrer.stripe_account_id or not referrer.stripe_charges_enabled:
        return "payout_skipped_no_stripe"

    if not hasattr(referrer, "billing_profile"):
        return "payout_skipped_no_billing"

    profile = referrer.billing_profile
    if not profile.self_billing_agreed:
        return "payout_skipped_no_agreement"

    missing_fields = [f for f in ("billing_name", "vat_country_code", "billing_address") if not getattr(profile, f)]
    if missing_fields:
        logger.info(
            "payout_skipped_incomplete_billing",
            referrer_id=str(referrer.id),
            payout_id=str(payout.id),
            missing=missing_fields,
        )
        return "payout_skipped_incomplete_billing"

    if payout.payout_amount < settings.MINIMUM_PAYOUT_AMOUNT:
        logger.info(
            "payout_skipped_below_threshold",
            referrer_id=str(referrer.id),
            payout_id=str(payout.id),
            amount=str(payout.payout_amount),
            threshold=str(settings.MINIMUM_PAYOUT_AMOUNT),
        )
        return "payout_skipped_below_threshold"

    return None


@shared_task(name="accounts.process_referral_payouts")
def process_referral_payouts() -> dict[str, int]:
    """Process all CALCULATED referral payouts via Stripe Transfer.

    For each payout:
    1. Claim the row (``CALCULATED → PENDING`` under ``select_for_update``).
    2. Verify referrer has a connected Stripe account, a billing profile,
       and has agreed to self-billing.
    3. Create a Stripe Transfer (with idempotency key ``payout.id``).
    4. On success → ``PAID``; on Stripe error → ``FAILED`` (logged, loop continues).
    5. Dispatch the per-payout statement+email task (retryable, idempotent) so a
       transient statement/email failure self-heals instead of aborting the batch.

    Errors are handled per-payout so one failure does not block the rest.

    Returns:
        Dict with ``paid``, ``failed``, and ``skipped`` counts.
    """
    _reclaim_stale_payouts()
    _redispatch_missing_statements()

    payout_ids = list(
        ReferralPayout.objects.filter(status=ReferralPayout.ReferralPayoutStatus.CALCULATED)
        .order_by("period_start")
        .values_list("id", flat=True)
    )

    stats: dict[str, int] = {"paid": 0, "failed": 0, "skipped": 0}

    for payout_id in payout_ids:
        # Claim the payout under a row lock to prevent duplicate processing.
        with transaction.atomic():
            payout = (
                ReferralPayout.objects.select_for_update(skip_locked=True)
                .filter(id=payout_id, status=ReferralPayout.ReferralPayoutStatus.CALCULATED)
                .first()
            )
            if payout is None:
                continue
            payout.status = ReferralPayout.ReferralPayoutStatus.PENDING
            payout.save(update_fields=["status", "updated_at"])

        # Reload with relations outside the lock
        payout = ReferralPayout.objects.select_related("referral__referrer__billing_profile", "referral__referrer").get(
            id=payout_id
        )
        referrer = payout.referral.referrer

        skip_reason = _validate_payout_eligibility(payout, referrer)
        if skip_reason:
            if skip_reason not in ("payout_skipped_incomplete_billing", "payout_skipped_below_threshold"):
                logger.info(skip_reason, referrer_id=str(referrer.id), payout_id=str(payout.id))
            payout.status = ReferralPayout.ReferralPayoutStatus.CALCULATED
            payout.save(update_fields=["status", "updated_at"])
            stats["skipped"] += 1
            continue

        # --- Stripe Transfer (idempotent via payout ID key) ---
        try:
            transfer = stripe.Transfer.create(
                amount=to_stripe_amount(payout.payout_amount, payout.currency),
                currency=payout.currency.lower(),
                destination=referrer.stripe_account_id,
                transfer_group=f"referral-payout-{payout.id}",
                idempotency_key=f"referral-payout-{payout.id}",
            )
        except stripe.error.StripeError as exc:
            payout.status = ReferralPayout.ReferralPayoutStatus.FAILED
            payout.save(update_fields=["status", "updated_at"])
            stats["failed"] += 1

            logger.error(
                "payout_transfer_failed",
                payout_id=str(payout.id),
                referrer_id=str(referrer.id),
                error=str(exc),
            )
            continue

        payout.status = ReferralPayout.ReferralPayoutStatus.PAID
        payout.stripe_transfer_id = transfer.id
        payout.save(update_fields=["status", "stripe_transfer_id", "updated_at"])
        stats["paid"] += 1

        logger.info(
            "payout_transferred",
            payout_id=str(payout.id),
            transfer_id=transfer.id,
            amount=str(payout.payout_amount),
            currency=payout.currency,
        )

        # --- Dispatch statement + email (retryable, only after successful transfer) ---
        # Done out-of-band so a transient statement/email failure self-heals via
        # Celery retry instead of aborting the batch or silently dropping the
        # statement (a financial document). The transfer is already committed; a
        # missed dispatch is recovered by the backstop sweep on the next run.
        _dispatch_statement(str(payout.id))

    logger.info("process_referral_payouts_completed", **stats)
    return stats


@shared_task(
    name="accounts.generate_and_send_payout_statement",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=3,
)
def generate_and_send_payout_statement(payout_id: str) -> None:
    """Generate a payout statement PDF and email it to the referrer.

    Dispatched per-payout after a successful Stripe transfer (see
    :func:`process_referral_payouts`). Retryable and idempotent so a transient
    storage/render/SMTP failure self-heals instead of silently dropping the
    statement — which, for a self-billing arrangement, is a financial document.
    ``generate_payout_statement`` returns the existing statement if one was
    already created, so retries never duplicate it.

    Args:
        payout_id: UUID (as ``str``) of the PAID ``ReferralPayout``.
    """
    from accounts.service.payout_statement_service import generate_payout_statement

    payout = ReferralPayout.objects.select_related("referral__referrer__billing_profile", "referral__referrer").get(
        id=payout_id
    )
    # Guard the financial-document invariant: this public task must never issue a
    # statement for a payout that wasn't actually paid (e.g. a stale beat row or a
    # manual dispatch handing it a CALCULATED/FAILED id). Callers only dispatch
    # PAID payouts, so this is a fail-fast safety net, not an expected path.
    if payout.status != ReferralPayout.ReferralPayoutStatus.PAID:
        logger.warning("payout_statement_skipped_non_paid", payout_id=payout_id, status=payout.status)
        return
    referrer = payout.referral.referrer
    statement = generate_payout_statement(payout)
    _send_payout_statement_email(payout, statement, referrer)


def _send_payout_statement_email(
    payout: ReferralPayout,
    statement: ReferralPayoutStatement,
    referrer: RevelUser,
) -> None:
    """Dispatch the payout statement PDF to the referrer via email."""
    billing_profile = getattr(referrer, "billing_profile", None)
    recipient = billing_profile.billing_email if billing_profile and billing_profile.billing_email else referrer.email

    subject = _("Referral payout statement %(document_number)s (%(currency)s)") % {
        "document_number": statement.document_number,
        "currency": payout.currency,
    }
    body = _(
        "Please find attached your referral payout statement %(document_number)s "
        "for the period %(period_start)s to %(period_end)s."
    ) % {
        "document_number": statement.document_number,
        "period_start": payout.period_start.isoformat(),
        "period_end": payout.period_end.isoformat(),
    }

    site = SiteSettings.get_solo()
    bcc = [site.platform_invoice_bcc_email] if site.platform_invoice_bcc_email else []

    send_email(
        to=recipient,
        subject=subject,
        body=body,
        bcc=bcc,
        from_email=settings.DEFAULT_BILLING_EMAIL,
        reply_to=[settings.DEFAULT_REPLY_TO_EMAIL],
        attachment_storage_path=statement.pdf_file.name,
        attachment_filename=f"{statement.document_number}.pdf",
    )

    statement.mark_email_sent()

    logger.info(
        "payout_statement_email_sent",
        document_number=statement.document_number,
        referrer_id=str(referrer.id),
    )
