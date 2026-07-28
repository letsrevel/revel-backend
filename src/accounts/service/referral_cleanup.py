"""Referral-aware teardown for account deletion (issue #796).

``RevelUser`` is referenced by three referral FKs. Two of them are still
``PROTECT`` on purpose — ``ReferralCode.user`` and ``Referral.referrer`` — because
those rows are *deleted*, never orphaned. The other two edges are ``SET_NULL``
(``Referral.referred_user``, ``ReferralPayout.referral``) so financial history
survives its subject leaving.

What survives a deletion, and why:

- ``ReferralPayoutStatement`` rows are issued Gutschriften / payout statements
  with sequential document numbers — 7-year retention under BAO §132, which is
  an explicit GDPR Art. 17(3)(b) exemption. They already snapshot referrer
  identity, so they stay meaningful without the user row.
- ``PAID`` ``ReferralPayout`` rows hold the ``stripe_transfer_id`` and are
  ``PROTECT``-ed by their statement, so they are retained with ``referral=NULL``.
- Everything else (unpaid payouts, ``Referral`` rows, the ``ReferralCode``) is
  deleted.

This module is the authoritative implementation — the endpoint's force-confirmation
check (:func:`assess_referral_forfeiture`) is UX only. :func:`cleanup_referral_data`
is idempotent and safe to re-run.
"""

import typing as t
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Q, QuerySet
from django.template.loader import render_to_string

from accounts.exceptions import ReferralPayoutInFlightError
from accounts.models import Referral, ReferralCode, ReferralPayout, RevelUser, UserBillingProfile
from common.models import SiteSettings
from common.tasks import send_email

logger = structlog.get_logger(__name__)

_Status = ReferralPayout.ReferralPayoutStatus

#: Statuses whose amounts are actually forfeited. ``ROLLED_OVER`` payouts are
#: deleted too, but their amounts were already folded into a later ``CALCULATED``
#: row's ``rolled_over_amount`` — counting both would double-count.
FORFEITABLE_STATUSES: frozenset[str] = frozenset({_Status.CALCULATED, _Status.FAILED})


@dataclass(frozen=True)
class ForfeitedPayoutItem:
    """One payout line lost to account deletion, for the admin audit trail."""

    payout_id: str
    period_start: date
    period_end: date
    amount: Decimal
    currency: str
    status: str


@dataclass(frozen=True)
class ReferralForfeitureSummary:
    """Consequences of deleting a referrer's account.

    ``unpaid_total`` and ``unpaid_count`` cover ``CALCULATED`` + ``FAILED`` rows
    only (see :data:`FORFEITABLE_STATUSES`); ``failed_total`` is the ``FAILED``
    subset, surfaced separately so the user can contact support before deleting.
    """

    unpaid_count: int = 0
    unpaid_total: Decimal = Decimal("0.00")
    failed_total: Decimal = Decimal("0.00")
    currency: str = settings.DEFAULT_CURRENCY
    items: list[ForfeitedPayoutItem] = field(default_factory=list)


def _summarize(payouts: t.Sequence[ReferralPayout]) -> ReferralForfeitureSummary:
    """Build a forfeiture summary from the payouts that will be deleted."""
    doomed = [p for p in payouts if p.status != _Status.PAID]
    forfeited = [p for p in doomed if p.status in FORFEITABLE_STATUSES]
    return ReferralForfeitureSummary(
        unpaid_count=len(forfeited),
        unpaid_total=sum((p.payout_amount for p in forfeited), Decimal("0.00")),
        failed_total=sum((p.payout_amount for p in forfeited if p.status == _Status.FAILED), Decimal("0.00")),
        # ponytail: payouts are created in DEFAULT_CURRENCY by the monthly
        # calculation, so a single currency is accurate today. If per-referral
        # currencies ever land, this needs to become a per-currency breakdown.
        currency=forfeited[0].currency if forfeited else settings.DEFAULT_CURRENCY,
        items=[
            ForfeitedPayoutItem(
                payout_id=str(p.id),
                period_start=p.period_start,
                period_end=p.period_end,
                amount=p.payout_amount,
                currency=p.currency,
                status=p.status,
            )
            for p in doomed
        ],
    )


def assess_referral_forfeiture(user: RevelUser) -> ReferralForfeitureSummary | None:
    """Assess what deleting ``user`` would cost them as a *referrer*.

    Returns ``None`` when no explicit confirmation is needed — i.e. the user
    referred nobody. Users who were merely *referred*, and referrers whose code
    was never used, are cleaned up silently because they lose nothing.

    Args:
        user: The user about to be deleted.

    Returns:
        The forfeiture summary, or ``None`` if deletion is consequence-free.
    """
    if not Referral.objects.filter(referrer=user).exists():
        return None
    return _summarize(list(ReferralPayout.objects.filter(referral__referrer=user)))


def cleanup_referral_data(user: RevelUser) -> ReferralForfeitureSummary | None:
    """Detach and tear down ``user``'s referral graph so ``user.delete()`` can proceed.

    Idempotent — a user with no referral involvement is a no-op. Ordering matters:
    statements for ``PAID`` payouts are backfilled *before* the user (and their
    CASCADE'd billing profile) disappears, since the generator snapshots identity
    from it.

    Args:
        user: The user about to be deleted.

    Returns:
        The forfeiture summary if anything was forfeited, else ``None``.

    Raises:
        ReferralPayoutInFlightError: If a payout is ``PENDING`` (a Stripe transfer
            may be in flight). The caller must retry later.
    """
    payouts = ReferralPayout.objects.filter(referral__referrer=user)

    if payouts.filter(status=_Status.PENDING).exists():
        raise ReferralPayoutInFlightError(f"user {user.id} has pending referral payouts")

    _backfill_missing_statements(payouts)

    with transaction.atomic():
        locked = list(
            ReferralPayout.objects.select_for_update(of=("self",)).filter(referral__referrer=user).order_by("id")
        )
        # Re-check under the lock: the disbursement task may have claimed a row
        # between the pre-check and here.
        if any(p.status == _Status.PENDING for p in locked):
            raise ReferralPayoutInFlightError(f"user {user.id} has pending referral payouts")

        summary = _summarize(locked)
        doomed_ids = [p.id for p in locked if p.status != _Status.PAID]
        retained_ids = [p.id for p in locked if p.status == _Status.PAID]

        ReferralPayout.objects.filter(id__in=doomed_ids).delete()
        # Retained for BAO §132 / GDPR Art. 17(3)(b): detach from the referral
        # row that is about to disappear. Explicit rather than relying on the
        # SET_NULL cascade below, so the intent survives future refactors.
        ReferralPayout.objects.filter(id__in=retained_ids).update(referral=None)

        Referral.objects.filter(referrer=user).delete()

        # The user's own referral (as the *referred* party) belongs to somebody
        # else's code: drop it only if it carries no payout history, otherwise
        # leave it for the ``referred_user`` SET_NULL on ``user.delete()``.
        own_referral = Referral.objects.filter(referred_user=user).first()
        if own_referral is not None and not own_referral.payouts.exists():
            own_referral.delete()

        ReferralCode.objects.filter(user=user).delete()

    logger.info(
        "referral_data_cleaned_up",
        user_id=str(user.id),
        forfeited_count=summary.unpaid_count,
        forfeited_total=str(summary.unpaid_total),
        failed_total=str(summary.failed_total),
        retained_paid_payouts=len(retained_ids),
    )

    if not summary.items:
        return None

    _notify_admins_of_forfeiture(user, summary)
    return summary


def _backfill_missing_statements(payouts: QuerySet[ReferralPayout]) -> None:
    """Issue statements for ``PAID`` payouts that never got one, synchronously.

    Must run before the user is deleted: the generator snapshots the referrer's
    billing profile, which CASCADEs away with the user.
    """
    from accounts.service.payout_statement_service import generate_payout_statement

    unissued = payouts.filter(status=_Status.PAID, statement__isnull=True).select_related(
        "referral__referrer__billing_profile", "referral__referrer"
    )
    for payout in unissued:
        try:
            generate_payout_statement(payout)
        except UserBillingProfile.DoesNotExist:
            # A PAID payout always had a billing profile at transfer time, so this
            # only happens if it was removed afterwards. Log loudly, but never let
            # it block an Art. 17 erasure — the transfer itself stays on record.
            logger.error("payout_statement_backfill_skipped_no_billing", payout_id=str(payout.id))


def _notify_admins_of_forfeiture(user: RevelUser, summary: ReferralForfeitureSummary) -> None:
    """Email staff the full itemization of payouts lost to this deletion.

    The forfeited rows are about to be deleted, so this email is the durable
    trace — notably for ``FAILED`` payouts, which represent money we owed and
    could not transfer.
    """
    admins = list(RevelUser.objects.filter(Q(is_superuser=True) | Q(is_staff=True)))
    if not admins:
        logger.warning("referral_forfeiture_no_admins_to_notify", user_id=str(user.id))
        return

    context = {
        "user_email": user.email,
        "user_id": str(user.id),
        "items": summary.items,
        "unpaid_count": summary.unpaid_count,
        "unpaid_total": summary.unpaid_total,
        "failed_total": summary.failed_total,
        "currency": summary.currency,
        "frontend_base_url": SiteSettings.get_solo().frontend_base_url,
    }
    subject = str(render_to_string("accounts/emails/referral_forfeiture_admin_subject.txt", context)).strip()
    body = render_to_string("accounts/emails/referral_forfeiture_admin_body.txt", context)
    html_body = render_to_string("accounts/emails/referral_forfeiture_admin_body.html", context)
    for admin in admins:
        send_email(to=admin.email, subject=subject, body=body, html_body=html_body)
