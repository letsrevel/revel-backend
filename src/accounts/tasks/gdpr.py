"""Tasks for GDPR user-data exports and account deletion."""

import re
import traceback
import typing as t
from datetime import datetime, timedelta

import structlog
from celery import Task, shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from accounts.exceptions import ReferralPayoutInFlightError
from accounts.models import RevelUser, UserDataExport
from accounts.service import gdpr
from accounts.service.gdpr import EXPORT_FILE_URL_EXPIRES_IN
from accounts.service.referral_cleanup import cleanup_referral_data
from common.models import SiteSettings
from common.signing import generate_signed_url
from common.tasks import send_email
from common.utils import render_plain_text

# 7 days in seconds for data export download links. Single source of truth lives
# in the service, which signs the file URLs *inside* the export for the same
# lifetime; this alias is kept because it is re-exported from accounts.tasks.
DATA_EXPORT_URL_EXPIRES_IN = EXPORT_FILE_URL_EXPIRES_IN

logger = structlog.get_logger(__name__)


@shared_task(name="accounts.tasks.generate_user_data_export")
def generate_user_data_export(user_id: str) -> None:
    """Generate a data export for a user, unless the account is erased or its deletion is pending.

    The export, its file and both emails are made while this task holds the account row lock (NO KEY
    UPDATE, the lock that the deletion task takes first). So an erasure either commits before the lock, and
    the export is skipped, or waits and then removes the finished export with the other rows of the account.
    An export never writes the profile of an account that an erasure already cleared, and never mails a
    download link after it.

    The lock is on one account only. It also holds back that account's own token refresh, sign-in and
    checkouts for the time of the export.
    """
    logger.info("gdpr_export_task_started", user_id=user_id)
    failure: Exception | None = None
    with transaction.atomic():
        user = RevelUser.objects.select_for_update(no_key=True).filter(id=user_id).first()
        if user is None or user.erased_at or user.deletion_requested_at:
            # Queued before the deletion: nothing personal may be written now, and nobody can receive it.
            logger.info("gdpr_export_skipped_account_deleted", user_id=user_id)
            return
        # Each failure is raised after the commit, so that the FAILED status (or the READY export) stays.
        try:
            with transaction.atomic():  # a savepoint: a database error here must not break the failure report
                data_export = gdpr.generate_user_data_export(user)
        except Exception as e:
            logger.error("gdpr_export_task_failed", user_id=user_id, error=str(e), exc_info=True)
            failure = e
            try:
                _notify_data_export_failed(user, traceback.format_exc())  # saves FAILED first, then sends mail
            except Exception:
                # A failed mail must not roll back the FAILED status; the task still raises the export error.
                logger.error("gdpr_export_failure_report_failed", user_id=user_id, exc_info=True)
        else:
            logger.info("gdpr_export_task_completed", user_id=user_id, export_id=str(data_export.id))
            try:
                _notify_user_data_export_ready(data_export)
            except Exception as e:
                failure = e
    if failure is not None:
        raise failure  # Celery marks the task as failed for monitoring and alerting


def _send_in_savepoint(**email: t.Any) -> None:
    """Send one export email in its own savepoint; the error of a failed send still reaches the caller.

    ``send_email`` writes its ``EmailLog`` rows in the transaction of the export. A database error in that
    write marks the transaction for rollback, and the commit would then lose the export status that is
    already written (FAILED or READY). The savepoint keeps the rollback to the email's own writes. The
    message is not sent again: it can already be out.
    """
    with transaction.atomic():
        send_email(**email)


def _notify_data_export_failed(user: RevelUser, error: str) -> None:
    logger.info("gdpr_export_notification_failed", user_id=str(user.id), email=user.email)
    data_export, _ = UserDataExport.objects.get_or_create(user=user)
    data_export.status = UserDataExport.UserDataExportStatus.FAILED
    data_export.error_message = error
    data_export.save(update_fields=["status", "error_message"])

    frontend_base_url = SiteSettings.get_solo().frontend_base_url

    # Celery workers have no active language: render each email in its recipient's.
    # Notify the user that something went wrong
    with translation.override(user.language):
        subject = str(render_plain_text("accounts/emails/data_export_failed_subject.txt"))
        body = render_plain_text("accounts/emails/data_export_failed_body.txt", {"user": user})
        html_body = render_to_string(
            "accounts/emails/data_export_failed_body.html",
            {"user": user, "frontend_base_url": frontend_base_url},
        )
    _send_in_savepoint(to=user.email, subject=subject, body=body, html_body=html_body)

    # Notify admins
    admins = RevelUser.objects.filter(Q(is_superuser=True) | Q(is_staff=True))
    admin_count = admins.count()
    logger.info("gdpr_export_admin_notification_sending", user_id=str(user.id), admin_count=admin_count)
    for admin in admins:
        with translation.override(admin.language):
            subject = str(render_plain_text("accounts/emails/data_export_failed_admin_subject.txt"))
            body = render_plain_text(
                "accounts/emails/data_export_failed_admin_body.txt",
                {"user": user, "error_message": data_export.error_message},
            )
            html_body = render_to_string(
                "accounts/emails/data_export_failed_admin_body.html",
                {"user": user, "error_message": data_export.error_message, "frontend_base_url": frontend_base_url},
            )
        _send_in_savepoint(to=admin.email, subject=subject, body=body, html_body=html_body)


def _notify_user_data_export_ready(data_export: UserDataExport) -> None:
    logger.info(
        "gdpr_export_notification_ready",
        user_id=str(data_export.user.id),
        email=data_export.user.email,
        export_id=str(data_export.id),
    )
    if not data_export.file.name:
        raise ValueError(f"Data export {data_export.id} has no file to link to")
    signed_path = generate_signed_url(data_export.file.name, expires_in=DATA_EXPORT_URL_EXPIRES_IN)
    download_url = settings.BASE_URL.rstrip("/") + signed_path
    frontend_base_url = SiteSettings.get_solo().frontend_base_url
    with translation.override(data_export.user.language):
        subject = str(_("Your Revel Data Export is Ready"))
        body = render_plain_text(
            "accounts/emails/data_export_ready_body.txt", {"download_url": download_url, "user": data_export.user}
        )
        html_body = render_to_string(
            "accounts/emails/data_export_ready_body.html",
            {"download_url": download_url, "user": data_export.user, "frontend_base_url": frontend_base_url},
        )
    _send_in_savepoint(to=data_export.user.email, subject=subject, body=body, html_body=html_body)


def _cancel_live_subscriptions(user: RevelUser) -> None:
    """Stop billing before the rows holding the Stripe handles are cascaded away.

    ``MembershipSubscription`` and ``CustomerProfile`` are ``on_delete=CASCADE``,
    so deleting the user destroys ``stripe_subscription_id`` — the only handle
    back to a Subscription that would otherwise keep charging their card on the
    organization's connected account, and one the nightly reconciliation (which
    walks local rows) can never rediscover.

    Runs *before* ``user.delete()`` and outside any surrounding atomic block, so
    ``cancel_subscriptions_for_membership_loss`` commits and fires its
    ``on_commit`` Stripe cancel while the rows still exist. That callback closes
    over the already-loaded subscription (and its ``select_related``
    organization), so it never re-reads a row the cascade is about to remove.

    Failures are logged and swallowed: a GDPR erasure must not be blocked by
    Stripe being unreachable. The identifiers needed to close a subscription by
    hand are emitted up front, since the local rows will not survive this task.
    """
    from events.models import MembershipSubscription
    from events.service.subscription import lifecycle as subscription_lifecycle

    live = list(
        MembershipSubscription.objects.filter(user=user)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .select_related("organization")
    )
    if not live:
        return
    logger.info(
        "account_deletion_cancelling_subscriptions",
        user_id=str(user.id),
        subscriptions=[
            {
                "subscription_id": str(subscription.pk),
                "organization_id": str(subscription.organization_id),
                "stripe_subscription_id": subscription.stripe_subscription_id,
            }
            for subscription in live
        ],
    )
    organizations = {subscription.organization_id: subscription.organization for subscription in live}
    for organization in organizations.values():
        try:
            subscription_lifecycle.cancel_subscriptions_for_membership_loss(user, organization)
        except Exception:
            logger.exception(
                "account_deletion_subscription_cancel_failed",
                user_id=str(user.id),
                organization_id=str(organization.pk),
            )


@shared_task(
    bind=True,
    name="accounts.tasks.delete_user_account",
    autoretry_for=(ReferralPayoutInFlightError,),
    retry_backoff=300,
    retry_backoff_max=3600,
    max_retries=12,
)
def delete_user_account(
    self: Task,  # type: ignore[type-arg]
    user_id: str,
    force_full_delete: bool = False,
    deletion_mark: str | None = None,
) -> None:
    """Delete a user account and all associated data in the background.

    This task is designed to handle heavy deletion operations that may involve
    many database relationships. It is the one deletion path: self-service
    deletion, the unverified-account sweep and the admin all run it.

    An account that still holds issued tickets or series passes for an event
    that is upcoming or ended less than 120 days ago is not deleted: it is
    erased in place (see :mod:`accounts.service.account_erasure`), so that the
    holdings stay valid. A second run on such an erased account does nothing.
    ``force_full_delete`` (the superuser admin action) deletes the row anyway.

    Any live membership subscription is cancelled first — see
    :func:`_cancel_live_subscriptions`, which must stay *outside* the
    transaction so its ``on_commit`` Stripe cancel actually fires. Referral
    teardown follows as its own independently-committed phase (referral rows
    PROTECT the user, so the graph must go before the cascade — see
    :mod:`accounts.service.referral_cleanup`): the cleanup issues statement
    PDFs, whose file writes cannot be rolled back, and holding row locks across
    the whole cascade would fight the disbursement task. Safe because both
    phases are idempotent — if the process dies between them, or the task is
    retried or re-triggered, the second run re-derives the state, finds nothing
    left to tear down, and proceeds to the delete.

    The cascade and the simple-history purge then run inside **one** explicit
    transaction. Both halves are needed for the erasure to be complete, and a
    Celery worker runs in autocommit (``ATOMIC_REQUESTS`` only covers HTTP), so
    without this ``user.delete()`` would commit on its own and a crash before
    the purge would strand history rows carrying the user's pk with no retry
    path — a retry finds no account and skips it. The purge runs
    after the delete, because the cascade itself writes one deletion row per
    removed live row (see :func:`~accounts.service.gdpr.purge_user_history`).

    A payout that is mid-transfer defers the whole deletion via Celery retry
    rather than racing Stripe.

    The self-service confirm deactivated the account and set ``deletion_requested_at`` before it queued this
    task with that mark (``deletion_mark``). When the task fails for the last time (any exception that it
    raises, or the payout still in flight after the last retry), it logs ``account_deletion_failed``, and an
    account that still has exactly that mark becomes active again, so the user is not locked out of an account
    that still exists. A run without a mark (the admin entry points, the unverified sweep, a message queued by
    older code) never reactivates. The admin entry points and a ban also clear the mark of the row. See
    :func:`_after_final_failure`. A run that never reports (a hard kill, a lost message, a failed dispatch)
    leaves the mark, and :func:`requeue_stale_account_deletions` queues the deletion again.

    A run with a mark does its work only while the account is inactive and still has exactly that mark. The
    task reads the account again and checks this at three points: immediately before the subscription cancel,
    immediately before the referral cleanup, and under the account row lock before it releases capacity and
    erases or deletes. When the mark is gone or is another one (the access was given back, an admin
    reactivated the account, the sweep queued a newer run, a ban or an admin delete queued its own run), the
    run logs ``account_deletion_skipped_mark_changed`` and ends: it starts no further step, restores nothing
    and is not retried. A run without a mark has no such check.

    The exact contract: a step does not start after the check before it failed. The row lock is held only
    for the last phase, so the erase or delete can never apply to an account whose state changed. The
    subscription cancel and the referral cleanup run without that lock (the cleanup writes statement PDFs and
    takes the locks of the payout task, and the cancel must commit on its own). A change that commits after
    the check of one of these two steps and before the step ends does not stop that step: the step that is
    already running completes, and every later step is skipped.

    Args:
        self: The bound task (its retry count decides a final failure).
        user_id: The UUID of the user to delete.
        force_full_delete: Delete the row and every holding even when the account keeps some.
        deletion_mark: The ``deletion_requested_at`` value (ISO 8601) that this run was queued for, if any.
    """
    mark = datetime.fromisoformat(deletion_mark) if deletion_mark else None
    try:
        _delete_or_erase(user_id, force_full_delete, mark)
    except ReferralPayoutInFlightError:
        # Benign: a Stripe transfer is in flight. Retried with backoff; stale
        # PENDING rows are reclaimed after an hour by process_referral_payouts.
        logger.info("account_deletion_deferred_pending_payout", user_id=user_id)
        if self.request.retries >= t.cast(int, self.max_retries):
            _after_final_failure(user_id, mark)
        raise
    except Exception:
        # Only an exception reaches this. A hard kill (time limit, child OOM) or a lost message leaves the
        # account inactive with its mark: requeue_stale_account_deletions picks it up.
        _after_final_failure(user_id, mark)
        raise


def account_deletion_dispatch(user_id: str, force_full_delete: bool = False) -> t.Callable[[], None]:
    """A named on-commit callback that queues one account deletion without a mark (an admin or a ban run).

    The request has already answered when it runs, so a failed dispatch cannot reach the caller. It is
    logged as an error with the account id instead: the account stays deactivated, and an operator re-queues
    its deletion. It is a named function, not a ``partial``, so Django's robust handling can name it.
    """

    def dispatch_account_erasure() -> None:
        try:
            # The flag only when set: a worker that still runs the old signature (rolling deploy) then
            # accepts every normal deletion.
            if force_full_delete:
                delete_user_account.delay(user_id, force_full_delete=True)
            else:
                delete_user_account.delay(user_id)
        except Exception:
            logger.error("account_erasure_dispatch_failed", user_id=user_id, exc_info=True)

    return dispatch_account_erasure


def _delete_or_erase(user_id: str, force_full_delete: bool, mark: datetime | None) -> None:
    """The deletion itself: full delete, or erasure in place when the account keeps holdings.

    The account lookup is part of it, so a failed lookup also reaches the final-failure handling. ``mark`` is
    the mark that the run was queued for: see :func:`_mark_changed`.
    """
    from events.service.account_deletion_service import kept_holdings, release_capacity_of_account

    user = RevelUser.objects.filter(id=user_id).first()
    if user is None:  # already deleted, for example by an earlier queued run
        logger.info("account_deletion_skipped_missing", user_id=user_id)
        return
    if _mark_changed(user, mark):
        return
    if _keeps_erased_row(user, force_full_delete):
        return
    # Deliberately no email here: this log outlives the erasure it records.
    logger.info("account_deletion_started", user_id=str(user.id))
    from accounts.service.account_erasure import (
        delete_data_export_file_after_commit,
        delete_profile_files_after_commit,
        erase_account_keeping_holdings,
    )
    from events.service.member_register_service import suppress_mirror

    # ``delete()`` nulls the instance pk, so keep it for the history purge.
    pk = user.pk
    # Each of the two steps below has its own check on a fresh read, immediately before it. No lock is held
    # across them (see the task docstring), so a change after a check does not stop the step that it guards.
    if _mark_changed_since_read(pk, mark):
        return
    # The cancel flips the member to CANCELLED, which would close the register record;
    # the legal log must stay open (user nulled by SET_NULL on delete).
    with suppress_mirror():
        _cancel_live_subscriptions(user)
    if _mark_changed_since_read(pk, mark):
        return
    cleanup_referral_data(user)
    with transaction.atomic():
        # The account row first, read again: an admin change post then commits before this transaction (its
        # rows and fields are seen below) or waits and is refused (RevelUserChangeForm). NO KEY: FK checks of
        # inserts still pass.
        locked = RevelUser.objects.select_for_update(no_key=True).filter(pk=pk).first()
        if locked is None:  # deleted meanwhile, for example by an earlier queued run
            logger.info("account_deletion_skipped_missing", user_id=user_id)
            return
        user = locked
        if _mark_changed(user, mark):  # a change that committed after the first check, seen under the lock
            return
        if _keeps_erased_row(user, force_full_delete):  # an overlapping run erased it while this one waited
            return
        # The export row cascades on both paths below; its file does not go with the row.
        delete_data_export_file_after_commit(user)
        # Unfinished (PENDING) checkouts cascade with the account: give back their places and discount uses.
        release_capacity_of_account(user)
        # Read under the locks that the release holds, so no status changes in between.
        kept = None if force_full_delete else kept_holdings(user, timezone.now())
        if kept:
            erase_account_keeping_holdings(user, kept)
        else:
            delete_profile_files_after_commit(user)
            user.delete()
        gdpr.purge_user_history(pk)
    logger.info("account_deletion_completed", user_id=user_id, erased_in_place=bool(kept))


def _mark_changed(user: RevelUser, mark: datetime | None) -> bool:
    """Whether a run that was queued for ``mark`` must stop: the account is no longer waiting for that run.

    A run with a mark continues only while the account is inactive and still has exactly that mark. Each
    change of that state means that this run is no longer wanted: the access was given back after a failure,
    an admin reactivated or deactivated the account, the sweep gave the account a new mark and a new run, or
    a ban or an admin delete cleared the mark and queued its own run. A run without a mark (admin, ban, the
    unverified sweep, a message from older code) is never stopped here.
    """
    if mark is None or (not user.is_active and user.deletion_requested_at == mark):
        return False
    logger.info("account_deletion_skipped_mark_changed", user_id=str(user.pk))
    return True


def _mark_changed_since_read(pk: t.Any, mark: datetime | None) -> bool:
    """:func:`_mark_changed` on a fresh read of the account. A run without a mark reads nothing."""
    if mark is None:
        return False
    fresh = RevelUser.objects.filter(pk=pk).only("is_active", "deletion_requested_at").first()
    if fresh is None:  # deleted meanwhile by another run
        logger.info("account_deletion_skipped_missing", user_id=str(pk))
        return True
    return _mark_changed(fresh, mark)


def _keeps_erased_row(user: RevelUser, force_full_delete: bool) -> bool:
    """Whether ``user`` is already erased and still keeps holdings: a second run then does nothing.

    A second erasure would copy the placeholder email and the empty name over the kept holder snapshot.
    """
    from events.service.account_deletion_service import kept_holdings

    if user.erased_at and not force_full_delete and kept_holdings(user, timezone.now()):
        logger.info("account_deletion_skipped_erased_with_holdings", user_id=str(user.pk))
        return True
    return False


def _after_final_failure(user_id: str, mark: datetime | None) -> None:
    """Log the failed deletion and, for a self-service request, make the account active again.

    See :func:`~accounts.service.account_erasure.restore_access_after_failed_deletion` for which accounts
    qualify. A run without a mark never reactivates, and an admin run also clears the mark of the row first.
    """
    from accounts.service.account_erasure import restore_access_after_failed_deletion

    # The user id only: the error text can carry personal data, and Celery logs the traceback.
    logger.error("account_deletion_failed", user_id=user_id)
    restore_access_after_failed_deletion(user_id, mark)


# A mark older than this has no live run. One run lives at most about 11 hours: 12 retries with a backoff that
# is capped at 1 hour (about 9.5 hours in total), and each of the 13 attempts can use the 300 s task time limit.
# 24 hours leaves room for a slow queue. A second run for a live one would be safe (the runs serialise on the
# account row lock and the second finds the work done), but only one of them could give the access back.
STALE_ACCOUNT_DELETION_AFTER = timedelta(hours=24)
# ponytail: one sweep requeues at most this many accounts, oldest mark first; the next sweep (15 minutes) takes
# the next ones. Upgrade path: keyset pagination in one run, if stale marks ever come in hundreds.
STALE_ACCOUNT_DELETION_BATCH = 100


@shared_task(name="accounts.requeue_stale_account_deletions")
def requeue_stale_account_deletions() -> dict[str, int]:
    """Queue the deletion again for each account whose self-service deletion never ran to an end.

    ``confirm_account_deletion`` deactivates the account and sets ``deletion_requested_at`` before it queues
    :func:`delete_user_account`. A run that deletes or erases the account removes the mark, and so does the
    restore after a final failure. A mark that is older than :data:`STALE_ACCOUNT_DELETION_AFTER` therefore has
    no live run: the dispatch failed, the message was lost, the worker was killed, or the restore was refused
    (a globally banned email). Without this sweep that account stays inactive and undeleted.

    Each account gets a new mark with a compare-and-set UPDATE, and the new run carries the new mark. So two
    overlapping sweeps queue one run, and a late failure of an old run (it carries the old mark) cannot give
    the access back while the new run is queued. Accounts without a mark are never selected: an admin
    deactivation, an admin delete and a ban clear it, and an erasure clears it too.

    Returns:
        Dict with the count of deletions queued again.
    """
    cutoff = timezone.now() - STALE_ACCOUNT_DELETION_AFTER
    stale = RevelUser.objects.filter(is_active=False, erased_at__isnull=True, deletion_requested_at__lt=cutoff)
    candidates = list(
        stale.order_by("deletion_requested_at").values_list("pk", "deletion_requested_at")[
            :STALE_ACCOUNT_DELETION_BATCH
        ]
    )
    requeued = 0
    for pk, old_mark in candidates:
        new_mark = timezone.now()
        if not stale.filter(pk=pk, deletion_requested_at=old_mark).update(deletion_requested_at=new_mark):
            continue  # changed meanwhile: another sweep took it, or an admin, a ban or a run cleared the mark
        try:
            delete_user_account.delay(str(pk), deletion_mark=new_mark.isoformat())
        except Exception:
            # Not queued: put the old mark back, so the next sweep tries again and not only in 24 hours.
            RevelUser.objects.filter(pk=pk, deletion_requested_at=new_mark).update(deletion_requested_at=old_mark)
            raise
        requeued += 1
        logger.warning("account_deletion_requeued", user_id=str(pk))
    logger.info("stale_account_deletions_requeued", requeued=requeued)
    return {"requeued": requeued}


# An export file without a row is deleted only when it is older than the lifetime of a download link. An
# export that is being made now (its row is not committed yet) is minutes old, so it is never touched.
ORPHAN_DATA_EXPORT_MIN_AGE = timedelta(seconds=DATA_EXPORT_URL_EXPIRES_IN)
# ponytail: one sweep deletes at most this many files; the next sweep (one day later) takes the next ones.
# Upgrade path: a paginated listing, if the export folder ever holds thousands of orphan files.
ORPHAN_DATA_EXPORT_BATCH = 500
# The name that ``gdpr.generate_user_data_export`` gives its ZIP (the storage can add a suffix before ".zip").
_EXPORT_FILE_NAME = re.compile(r"revel_export_[0-9a-f-]{36}.*\.zip")


@shared_task(name="accounts.sweep_orphan_data_exports")
def sweep_orphan_data_exports() -> dict[str, int]:
    """Delete export ZIPs that no ``UserDataExport`` row names.

    :func:`generate_user_data_export` writes the ZIP inside the transaction that holds the account lock. A
    worker that is killed after the write and before the commit loses the row, and the file stays in storage.
    The expiry sweep and the account deletion find files only through rows, so nothing else removes it.

    The sweep lists the upload folder of ``UserDataExport.file`` in its storage. It deletes a file when it has
    the name of an export ZIP, no row names it, and it is older than :data:`ORPHAN_DATA_EXPORT_MIN_AGE`. The
    log has counts only: a file name has the account id.

    Returns:
        Dict with the count of files deleted.
    """
    field: t.Any = UserDataExport._meta.get_field("file")
    storage, folder = field.storage, str(field.upload_to).rstrip("/")
    try:
        _folders, names = storage.listdir(folder)
    except FileNotFoundError:  # no export was ever made on this storage
        names = []
    referenced = set(UserDataExport.objects.exclude(file="").exclude(file=None).values_list("file", flat=True))
    cutoff = timezone.now() - ORPHAN_DATA_EXPORT_MIN_AGE
    deleted = failed = 0
    for name in sorted(names):
        path = f"{folder}/{name}"
        if path in referenced or not _EXPORT_FILE_NAME.fullmatch(name):
            continue
        try:
            if storage.get_modified_time(path) >= cutoff:
                continue
            storage.delete(path)
        except FileNotFoundError:  # removed meanwhile by the expiry sweep or by an export cleanup
            continue
        except OSError as e:  # ponytail: FileSystemStorage only; a remote storage needs its own error types here
            # One bad file must not block the others on every run. The error type only: the path has the account id.
            failed += 1
            logger.error("orphan_data_export_delete_failed", error_type=type(e).__name__)
            continue
        deleted += 1
        if deleted >= ORPHAN_DATA_EXPORT_BATCH:
            break
    logger.info("orphan_data_exports_swept", files_deleted=deleted, files_failed=failed)
    return {"files_deleted": deleted, "files_failed": failed}


@shared_task(name="accounts.tasks.cleanup_expired_data_exports")
def cleanup_expired_data_exports() -> dict[str, int]:
    """Delete files from expired data exports while preserving database records.

    Data export download links are valid for 7 days. After that, the file is no longer
    accessible via signed URL, so we can safely delete it to reclaim storage.

    The database record is preserved for auditing purposes (shows user requested an export).

    Returns:
        Dict with count of files deleted.
    """
    now = timezone.now()
    expiry_cutoff = now - timedelta(seconds=DATA_EXPORT_URL_EXPIRES_IN)

    # Find exports with files that are older than the URL expiry time
    expired_exports = UserDataExport.objects.filter(
        completed_at__lte=expiry_cutoff,
        status=UserDataExport.UserDataExportStatus.READY,
    ).exclude(file="")

    count = 0
    for export in expired_exports:
        export.file.delete(save=False)
        export.file = None
        export.save(update_fields=["file", "updated_at"])
        count += 1
        logger.info(
            "data_export_file_deleted",
            export_id=str(export.id),
            user_id=str(export.user_id),
            completed_at=export.completed_at.isoformat() if export.completed_at else None,
        )

    logger.info("data_export_cleanup_completed", files_deleted=count)
    return {"files_deleted": count}


@shared_task(
    name="accounts.retry_account_file_delete",
    autoretry_for=(Exception,),
    max_retries=6,
    retry_backoff=60,
    retry_backoff_max=3600,
)
def retry_account_file_delete(*, user_id: str, files: list[tuple[str, str]]) -> None:
    """Delete the stored files of a deleted or erased account that the first delete after commit could not delete.

    ``files`` holds (field name, storage path) pairs in the default storage. A failure raises, so Celery tries
    again with backoff (about 2 hours in all); the last failure is logged by Celery with the task arguments,
    which hold ids and paths only.
    """
    from django.core.files.storage import default_storage

    for _field_name, file_name in files:
        default_storage.delete(file_name)
    logger.info("account_file_delete_retried", user_id=user_id, count=len(files))
