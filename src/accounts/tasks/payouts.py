"""Task for processing referral payouts via Stripe Transfer."""

from datetime import timedelta

import stripe
import structlog
from celery import shared_task
from django.conf import settings
from django.db import transaction
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
    5. Generate the payout statement PDF and email it to the referrer.

    Errors are handled per-payout so one failure does not block the rest.

    Returns:
        Dict with ``paid``, ``failed``, and ``skipped`` counts.
    """
    from accounts.service.payout_statement_service import generate_payout_statement

    _reclaim_stale_payouts()

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

        # --- Generate statement + email (only after successful transfer) ---
        statement = generate_payout_statement(payout)
        _send_payout_statement_email(payout, statement, referrer)

    logger.info("process_referral_payouts_completed", **stats)
    return stats


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

    logger.info(
        "payout_statement_email_sent",
        document_number=statement.document_number,
        referrer_id=str(referrer.id),
    )
