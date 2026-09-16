"""Referral program applications and admin invites.

One ``ReferralApplication`` row drives both flows (spec:
``docs/superpowers/specs/2026-09-16-referral-program-flows-design.md``). Every status
transition lives here; controllers and admin call in, never touch status directly.
"""

import uuid

import structlog
from django.db import transaction
from django.db.models import Q
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts import tasks
from accounts.exceptions import ReferralApplicationConflictError, ReferralApplicationsDisabledError
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
    """
    if not SiteSettings.get_solo().referral_applications_enabled:
        raise ReferralApplicationsDisabledError(str(_("Referral applications are not open.")))

    email = email.strip().lower()
    normalized = normalize_email_for_matching(email)
    clean_note = sanitize_note(note)
    if not clean_note:
        raise HttpError(422, str(_("A note is required.")))

    if ReferralApplication.objects.filter(
        normalized_email=normalized, status=ReferralApplication.Status.BLOCKED
    ).exists():
        logger.info("referral_application_blocked", email=email)
        return None
    if _is_enrolled(email):
        logger.info("referral_application_already_enrolled", email=email)
        return None

    assert_code_available(code)

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
