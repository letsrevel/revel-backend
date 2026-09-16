"""Referral program applications and admin invites.

One ``ReferralApplication`` row drives both flows (spec:
``docs/superpowers/specs/2026-09-16-referral-program-flows-design.md``). Every status
transition lives here; controllers and admin call in, never touch status directly.
"""

import uuid
from decimal import Decimal

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts import tasks
from accounts.exceptions import (
    ReferralAlreadyActiveError,
    ReferralApplicationConflictError,
    ReferralApplicationsDisabledError,
)
from accounts.models import ReferralApplication, ReferralCode, RevelUser
from accounts.utils.email_normalization import normalize_email_for_matching
from common.models import SiteSettings
from common.utils import get_or_create_with_race_protection

logger = structlog.get_logger(__name__)

_LIVE_STATUSES = (ReferralApplication.Status.PENDING, ReferralApplication.Status.APPROVED)


def sanitize_note(note: str) -> str:
    """Reduce a free-text note to plain text: tags stripped, whitespace collapsed at the ends."""
    return strip_tags(note).strip()


def assert_code_available(code: str, *, exclude_pk: uuid.UUID | None = None) -> None:
    """Raise a 409 conflict if ``code`` is held by a referral code or a live application (case-insensitive)."""
    if ReferralCode.objects.filter(code__iexact=code).exists():
        raise ReferralApplicationConflictError(str(_("This referral code is already taken.")))
    live = ReferralApplication.objects.filter(code__iexact=code, status__in=_LIVE_STATUSES)
    if exclude_pk is not None:
        live = live.exclude(pk=exclude_pk)
    if live.exists():
        raise ReferralApplicationConflictError(str(_("This referral code is already taken.")))


def _is_enrolled(email: str) -> bool:
    return RevelUser.objects.filter(email__iexact=email, referral_code__isnull=False).exists()


def submit_application(*, email: str, code: str, note: str) -> ReferralApplication | None:
    """Record a public application.

    Returns ``None`` (silently, with a log line) for blocked or already-enrolled emails so the
    endpoint cannot be used to probe enrollment state. Raises a 409 for a pending duplicate or a
    taken code, a 404 when applications are switched off.

    Order matters: the pending-duplicate and code-availability checks run *before* the silent
    ones, so the response only ever depends on facts the caller can already establish (is there
    a pending application for my own email, is this code taken). Checking enrollment first would
    turn a taken code into an oracle — 202 for an enrolled email, 409 for everyone else.
    """
    if not SiteSettings.get_solo().referral_applications_enabled:
        raise ReferralApplicationsDisabledError(str(_("Referral applications are not open.")))

    email = email.strip().lower()
    normalized = normalize_email_for_matching(email)
    clean_note = sanitize_note(note)
    if not clean_note:
        raise HttpError(422, str(_("A note is required.")))

    if ReferralApplication.objects.filter(
        normalized_email=normalized, status=ReferralApplication.Status.PENDING
    ).exists():
        raise ReferralApplicationConflictError(str(_("You already have a pending application.")))

    assert_code_available(code)

    if ReferralApplication.objects.filter(
        normalized_email=normalized, status=ReferralApplication.Status.BLOCKED
    ).exists():
        logger.info("referral_application_blocked", email=email)
        return None
    if _is_enrolled(email):
        logger.info("referral_application_already_enrolled", email=email)
        return None

    application, created = get_or_create_with_race_protection(
        ReferralApplication,
        Q(normalized_email=normalized, status=ReferralApplication.Status.PENDING),
        {"email": email, "code": code, "note": clean_note, "source": ReferralApplication.Source.APPLICATION},
    )
    if not created:
        raise ReferralApplicationConflictError(str(_("You already have a pending application.")))

    application_id = str(application.id)

    def _dispatch() -> None:
        tasks.send_account_email.delay(
            tasks.AccountEmail.REFERRAL_APPLICATION_RECEIVED, email, context={"code": application.code}
        )
        tasks.notify_admin_new_referral_application.delay(application_id=application_id)

    transaction.on_commit(_dispatch)
    logger.info("referral_application_submitted", application_id=application_id, email=email)
    return application


def _require_pending(application: ReferralApplication) -> ReferralApplication:
    """Re-read the row under lock and insist it is still PENDING."""
    locked = ReferralApplication.objects.select_for_update().get(pk=application.pk)
    if locked.status != ReferralApplication.Status.PENDING:
        raise ReferralApplicationConflictError(str(_("This application has already been decided.")))
    return locked


def _mark_decided(application: ReferralApplication, status: str, actor: RevelUser, admin_note: str | None) -> None:
    application.status = status
    application.decided_by = actor
    application.decided_at = timezone.now()
    if admin_note is not None:
        application.admin_note = admin_note
    application.save()


def _send_enrolled(email: str, code: ReferralCode) -> None:
    percent = (
        code.revenue_share_percent
        if code.revenue_share_percent is not None
        else settings.DEFAULT_REFERRAL_SHARE_PERCENT
    )
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.REFERRAL_ENROLLED,
            email,
            context={"code": code.code, "revenue_share_percent": f"{percent:.2f}"},
        )
    )


def _enroll(application: ReferralApplication, user: RevelUser) -> ReferralCode:
    """Create (or reactivate) the user's ReferralCode from the application and link the two."""
    existing = ReferralCode.objects.select_for_update().filter(user=user).first()
    if existing is not None and existing.is_active:
        raise ReferralAlreadyActiveError(str(_("{} already has an active referral code.").format(user.email)))
    if existing is not None:
        # ponytail: the user keeps their existing code, so ``application.code`` stays reserved by
        # an APPROVED row nobody owns (``assert_code_available`` will keep refusing it). Freeing or
        # reconciling it — rewriting the row's code to the kept one — is the upgrade path.
        existing.is_active = True
        existing.revenue_share_percent = application.revenue_share_percent
        existing.save(update_fields=["is_active", "revenue_share_percent", "updated_at"])
        code = existing
    else:
        code = ReferralCode.objects.create(
            user=user, code=application.code, revenue_share_percent=application.revenue_share_percent
        )
    application.user = user
    application.save(update_fields=["user", "updated_at"])
    _send_enrolled(user.email, code)
    logger.info("referral_enrolled", application_id=str(application.id), user_id=str(user.id), code=code.code)
    return code


@transaction.atomic
def approve(application: ReferralApplication, *, actor: RevelUser) -> ReferralApplication:
    """Approve a PENDING application: enroll now if the account exists, else email an invite."""
    application = _require_pending(application)
    _mark_decided(application, ReferralApplication.Status.APPROVED, actor, None)

    user = RevelUser.objects.filter(email__iexact=application.email).first()
    if user is not None:
        _enroll(application, user)
        return application

    application_id = str(application.id)
    context = {
        "code": application.code,
        "revenue_share_percent": f"{application.revenue_share_percent:.2f}",
        "admin_note": application.admin_note,
    }
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.REFERRAL_INVITE, application.email, token=application_id, context=context
        )
    )
    logger.info("referral_invite_sent", application_id=application_id, email=application.email)
    return application


def _decline(
    application: ReferralApplication, status: str, *, actor: RevelUser, admin_note: str
) -> ReferralApplication:
    application = _require_pending(application)
    _mark_decided(application, status, actor, sanitize_note(admin_note))
    note = application.admin_note
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.REFERRAL_REJECTED, application.email, context={"admin_note": note}
        )
    )
    logger.info("referral_application_declined", application_id=str(application.id), status=status)
    return application


@transaction.atomic
def reject(application: ReferralApplication, *, actor: RevelUser, admin_note: str) -> ReferralApplication:
    """Reject a PENDING application; the applicant may apply again later."""
    return _decline(application, ReferralApplication.Status.REJECTED, actor=actor, admin_note=admin_note)


@transaction.atomic
def block(application: ReferralApplication, *, actor: RevelUser, admin_note: str) -> ReferralApplication:
    """Permanently reject: further applications from this email are silently dropped."""
    return _decline(application, ReferralApplication.Status.BLOCKED, actor=actor, admin_note=admin_note)


@transaction.atomic
def create_invite(
    *, email: str, code: str, revenue_share_percent: Decimal, note: str, actor: RevelUser
) -> ReferralApplication:
    """Admin-initiated invite: a pre-approved application with ``source=INVITE``."""
    email = email.strip().lower()
    normalized = normalize_email_for_matching(email)
    if ReferralApplication.objects.filter(
        normalized_email=normalized, status=ReferralApplication.Status.BLOCKED
    ).exists():
        raise ReferralApplicationConflictError(str(_("This email is permanently blocked from the referral program.")))
    if ReferralApplication.objects.filter(
        normalized_email=normalized, status=ReferralApplication.Status.PENDING
    ).exists():
        raise ReferralApplicationConflictError(
            str(_("This email already has a pending application; decide it instead."))
        )
    assert_code_available(code)
    application = ReferralApplication.objects.create(
        email=email,
        code=code,
        revenue_share_percent=revenue_share_percent,
        admin_note=sanitize_note(note),
        source=ReferralApplication.Source.INVITE,
    )
    return approve(application, actor=actor)


def enroll_on_signup(user: RevelUser) -> ReferralCode | None:
    """Enroll a freshly created user who holds an APPROVED, not-yet-enrolled application."""
    application = (
        ReferralApplication.objects.select_for_update()
        .filter(
            normalized_email=normalize_email_for_matching(user.email),
            status=ReferralApplication.Status.APPROVED,
            user__isnull=True,
        )
        .order_by("-decided_at")
        .first()
    )
    if application is None:
        return None
    return _enroll(application, user)
