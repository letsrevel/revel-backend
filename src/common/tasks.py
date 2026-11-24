"""Common tasks."""

# Re-export thumbnail tasks for Celery autodiscovery
# (Celery only discovers tasks.py at app root, not in subdirectories)
import hashlib
import importlib
import typing as t
from datetime import timedelta
from pathlib import Path

import pyclamd
import structlog
from celery import shared_task
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from accounts.models import RevelUser
from common.email_backend import is_erased_address
from common.models import EmailLog, FileExport, FileUploadAudit, QuarantinedFile, SiteSettings
from common.thumbnails.config import THUMBNAIL_CONFIGS
from common.thumbnails.files import delete_unreferenced_files_after_commit, detach_image_everywhere, named_paths
from common.thumbnails.tasks import (  # noqa: F401
    delete_orphaned_thumbnails_task,
    generate_thumbnails_task,
)
from common.utils import render_plain_text, with_message_id

logger = structlog.get_logger(__name__)


_ALLOWED_EMAIL_CALLBACKS: frozenset[tuple[str, str]] = frozenset(
    {
        ("accounts.tasks", "mark_reminder_sent"),
    }
)


def _execute_email_callback(callback_data: dict[str, t.Any], success: bool, error_message: str | None) -> None:
    """Execute callback function after email delivery attempt.

    Args:
        callback_data: Callback configuration with module, function, and kwargs
        success: Whether email was sent successfully
        error_message: Error message if sending failed
    """
    try:
        module_path = callback_data.get("module")
        function_name = callback_data.get("function")
        kwargs = callback_data.get("kwargs", {})

        if not module_path or not function_name:
            logger.error("Invalid callback_data: missing module or function", callback_data=callback_data)
            return

        if (module_path, function_name) not in _ALLOWED_EMAIL_CALLBACKS:
            logger.error(
                "email_callback_blocked",
                module=module_path,
                function=function_name,
                reason="not in allowlist",
            )
            return

        # Import module and get function
        module = importlib.import_module(module_path)
        callback_function = getattr(module, function_name)

        # Add status and error_message to kwargs
        kwargs["success"] = success
        if not success:
            kwargs["error_message"] = error_message

        # Execute callback
        callback_function(**kwargs)

        logger.info(
            "email_callback_executed",
            module=module_path,
            function=function_name,
            success=success,
        )
    except Exception as callback_error:
        logger.error(
            "email_callback_failed",
            callback_data=callback_data,
            error=str(callback_error),
            exc_info=True,
        )


def preferences_list_unsubscribe_header() -> dict[str, str]:
    """``List-Unsubscribe`` header pointing at the notification preferences page (#1137).

    Brevo injects its own one-click header into any email lacking one, and a client
    "Unsubscribe" click then globally blocks the address. There is deliberately no
    ``List-Unsubscribe-Post``: clients open the page in a browser rather than POSTing
    silently, so nothing is switched off by the click.

    Returns:
        A ``List-Unsubscribe`` header only.
    """
    url = f"{SiteSettings.get_solo().frontend_base_url.rstrip('/')}/account/notifications"
    return {"List-Unsubscribe": f"<{url}>"}


@shared_task(name="common.tasks.send_email")
def send_email(
    *,
    to: str | list[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    bcc: list[str] | None = None,
    from_email: str | None = None,
    reply_to: list[str] | None = None,
    callback_data: dict[str, t.Any] | None = None,
    attachment_storage_path: str | None = None,
    attachment_filename: str | None = None,
    attachment_mime_type: str = "application/pdf",
    headers: dict[str, str] | None = None,
) -> None:
    """Send an email with optional callback and optional file attachment.

    Args:
        to: Email address(es).
        subject: Email subject.
        body: Plain text body.
        html_body: HTML body (optional).
        bcc: Explicit BCC addresses (optional). When not set, multiple "to"
            recipients are auto-BCC'd for privacy.
        from_email: Sender address. Defaults to ``settings.DEFAULT_FROM_EMAIL``.
        reply_to: Reply-To addresses (optional).
        callback_data: Optional callback configuration with:
            - module: Python module path (e.g., "accounts.tasks")
            - function: Function name to call (e.g., "mark_reminder_sent")
            - kwargs: Dict of keyword arguments to pass to the function
        attachment_storage_path: Path of the file in Django's default storage.
            The file is read inside the task worker to avoid passing large
            byte payloads through the Celery broker.
        attachment_filename: Display filename for the attachment (defaults to
            the storage path basename).
        attachment_mime_type: MIME type for the attachment.
        headers: Extra message headers (e.g. List-Unsubscribe, X-Mailin-custom). When no
            ``List-Unsubscribe`` is given, a preferences-page one is added so Brevo doesn't
            inject its own globally-blocking header (#1137).
    """
    from django.core.files.storage import default_storage

    success = False
    error_message = None

    try:
        site_settings = SiteSettings.get_solo()
        recipients = [to] if isinstance(to, str) else to
        # Erased accounts go out here, on the addresses as given (before the catch-all rewrite) and before the
        # To/BCC shape is chosen. The backend guard alone is too late for a batch: it sees the sender in To, so
        # a batch with only erased recipients would go to the sender.
        intended = len(recipients) + len(bcc or [])
        recipients = [email for email in recipients if not is_erased_address(email)]
        bcc = [email for email in (bcc or []) if not is_erased_address(email)]
        erased_dropped = intended - len(recipients) - len(bcc)
        recipients = [to_safe_email_address(email, site_settings=site_settings) for email in recipients]
        safe_bcc = [to_safe_email_address(email, site_settings=site_settings) for email in bcc]
        if erased_dropped and not recipients and not safe_bcc:
            # No address in the log: the placeholder names the account.
            logger.warning("email_dropped_no_recipient_left", subject=str(subject)[:120])
            success = True  # as for the single erased recipient that the backend guard drops
            return
        sender = from_email or settings.DEFAULT_FROM_EMAIL
        if not any(key.lower() == "list-unsubscribe" for key in headers or {}):
            headers = {**(headers or {}), **preferences_list_unsubscribe_header()}
        headers = with_message_id(headers, sender)

        # Build the To/BCC headers:
        # - Single recipient: use as To, add explicit BCC if provided.
        # - Multiple recipients: auto-BCC all for privacy (RFC 5322),
        #   merge with any explicit BCC addresses.
        # - Every To recipient erased, a BCC left: BCC only, with no sender in To (as the backend guard sends it).
        if len(recipients) == 1 or (erased_dropped and not recipients):
            email_msg = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=sender,
                to=recipients,
                bcc=safe_bcc,
                reply_to=reply_to or [],
                headers=headers,
            )
        else:
            email_msg = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=sender,
                to=[sender],
                bcc=recipients + safe_bcc,
                reply_to=reply_to or [],
                headers=headers,
            )

        if html_body:  # pragma: no branch
            email_msg.attach_alternative(html_body, "text/html")

        # Attach file from storage if requested
        if attachment_storage_path:
            with default_storage.open(attachment_storage_path, "rb") as f:
                attachment_bytes = f.read()
            filename = attachment_filename or Path(attachment_storage_path).name
            email_msg.attach(filename, attachment_bytes, attachment_mime_type)

        email_msg.send(fail_silently=False)

        # Create email logs
        email_logs: list[EmailLog] = []
        for recipient in recipients:
            el = EmailLog(to=recipient, subject=subject)
            el.set_body(body=body)
            if html_body:  # pragma: no branch
                el.set_html(html_body=html_body)
            email_logs.append(el)
        EmailLog.objects.bulk_create(email_logs)

        success = True
        logger.info("email_sent", to=recipients, subject=subject)

    except Exception as e:
        error_message = str(e)
        logger.error("email_send_failed", to=to, subject=subject, error=error_message, exc_info=True)
        raise  # Re-raise so Celery can retry if needed

    finally:
        # Execute callback if provided (even if failed)
        if callback_data:
            _execute_email_callback(callback_data, success, error_message)


@shared_task(name="common.tasks.cleanup_email_logs")
def cleanup_email_logs() -> None:
    """Clean up email logs."""
    older_than_a_week = EmailLog.objects.filter(sent_at__lte=timezone.now() - timedelta(days=7))
    older_than_a_week.delete()

    # delete compressed_body and compressed_html for older than a day
    older_than_a_day = EmailLog.objects.filter(sent_at__lte=timezone.now() - timedelta(days=1))
    older_than_a_day.update(compressed_body=None, compressed_html=None)


def to_safe_email_address(email: str, site_settings: SiteSettings | None = None) -> str:
    """Convert an email address to a safe format for sending.

    Args:
        email (str): The email address.
        site_settings (SiteSettings): The site settings.

    Returns:
        str: The safe email address.
    """
    site_settings = site_settings or SiteSettings.get_solo()
    if site_settings.live_emails:
        return email
    safe_email = email.replace("@", "_at_").replace(".", "_dot_")
    user, domain = site_settings.internal_catchall_email.split("@", 1)
    safe_email = f"{user}+{safe_email}@{domain}"
    return safe_email


@shared_task(name="common.tasks.scan_for_malware")
def scan_for_malware(*, app: str, model: str, pk: str, field: str) -> None | dict[str, t.Any]:
    """Scan for malware."""
    if not settings.FEATURE_MALWARE_SCAN:
        # Scope to this upload's still-pending audit: never rewrite another instance's row
        # or a previously-flagged (e.g. MALICIOUS) audit to CLEAN.
        FileUploadAudit.objects.filter(
            app=app,
            model=model,
            field=field,
            instance_pk=pk,
            status=FileUploadAudit.FileUploadAuditStatus.PENDING,
        ).update(status=FileUploadAudit.FileUploadAuditStatus.CLEAN, updated_at=timezone.now())
        return None
    cd = pyclamd.ClamdNetworkSocket(host=settings.CLAMAV_HOST, port=settings.CLAMAV_PORT)
    if not cd.ping():
        raise RuntimeError("ClamAV daemon is not reachable")
    model_class = apps.get_model(app, model)
    instance = model_class.objects.filter(pk=pk).first()
    # The files to scan: the path of each pending audit (the row can have a new file, or be gone, while a copy such
    # as a duplicated event still names the audited file), and the file that the row names now (audits from before
    # ``file_path`` existed).
    paths = set(
        FileUploadAudit.objects.filter(
            app=app, model=model, field=field, instance_pk=pk, status=FileUploadAudit.FileUploadAuditStatus.PENDING
        )
        .exclude(file_path="")
        .values_list("file_path", flat=True)
    )
    current = getattr(instance, field, None) if instance is not None else None
    if current:
        paths.add(current.name)
    if instance is None and not paths:
        raise model_class.DoesNotExist(f"{app}.{model} pk={pk} not found")
    result: dict[str, t.Any] | None = None
    for path in sorted(paths):
        findings = _scan_path(cd, model_class, app=app, model=model, pk=pk, field=field, path=path)
        result = findings or result
    return result


def _scan_path(
    cd: t.Any, model_class: t.Any, *, app: str, model: str, pk: str, field: str, path: str
) -> dict[str, t.Any] | None:
    """Scan one stored file of the upload ``(app, model, pk, field)``; quarantine it and detach it when flagged."""
    path_audits = FileUploadAudit.objects.filter(
        app=app,
        model=model,
        field=field,
        instance_pk=pk,
        file_path=path,
        status=FileUploadAudit.FileUploadAuditStatus.PENDING,
    )
    if not default_storage.exists(path):
        if named_paths([path]) or model_class.objects.filter(pk=pk, **{field: path}).exists():
            # A row still names a file that storage does not have (a storage fault?): keep the audit pending.
            raise FileNotFoundError("An uploaded file named by a row is missing from storage.")
        # Deleted meanwhile (for example replaced by a newer upload): nothing names or serves it. Close its audits,
        # so the rescan sweep does not queue them again.
        closed = path_audits.update(status=FileUploadAudit.FileUploadAuditStatus.CLEAN, updated_at=timezone.now())
        logger.info("upload_scan_file_gone", app=app, model=model, field=field, pk=pk, audits=closed)
        return None
    with default_storage.open(path, "rb") as stored:
        file_bytes = stored.read()
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    audited_hashes = set(path_audits.values_list("file_hash", flat=True))
    if audited_hashes and file_hash not in audited_hashes:
        # The path now holds another upload's bytes (a freed name was reused): the audited bytes are gone. That
        # upload's own scan handles these bytes; close the audits of this one.
        closed = path_audits.update(status=FileUploadAudit.FileUploadAuditStatus.CLEAN, updated_at=timezone.now())
        logger.info("upload_scan_path_reused", app=app, model=model, field=field, pk=pk, audits=closed)
        return None
    findings = cd.scan_stream(file_bytes)
    # Scope to this upload's audit rows: never quarantine another instance's file that happens
    # to share the same bytes, and never re-match an already-quarantined audit on re-upload.
    audit_qs = FileUploadAudit.objects.filter(app=app, model=model, field=field, file_hash=file_hash, instance_pk=pk)
    if not findings:
        audit_qs.filter(status=FileUploadAudit.FileUploadAuditStatus.PENDING).update(
            status=FileUploadAudit.FileUploadAuditStatus.CLEAN, updated_at=timezone.now()
        )
        return None
    name = Path(path).name
    # Only audits with no QuarantinedFile yet: a re-upload of identical bytes to the same
    # instance re-matches earlier audits whose quarantine row already exists, and bulk_create
    # writes each ContentFile to storage *before* the INSERT — including them would leave an
    # orphan blob per conflict. The status update is scoped to these same rows so an audit
    # inserted meanwhile is never marked MALICIOUS without a quarantine row to notify from.
    pending = list(audit_qs.filter(quarantinedfile__isnull=True))
    quarantined = [
        QuarantinedFile(audit=audit, file=ContentFile(file_bytes, name), findings=findings) for audit in pending
    ]
    meta = model_class._meta
    with transaction.atomic():
        audit_qs.filter(pk__in=[audit.pk for audit in pending]).update(
            status=FileUploadAudit.FileUploadAuditStatus.MALICIOUS, updated_at=timezone.now()
        )
        # ignore_conflicts: two concurrent scans of the same instance can still race on the
        # OneToOne; without this the IntegrityError would roll back the detachment below and
        # leave the malicious file live.
        QuarantinedFile.objects.bulk_create(quarantined, ignore_conflicts=True)
        if meta.get_field(field).null:
            # Nullable field: blank the reference, and delete the file after commit (the quarantine row holds the
            # bytes). Only the image columns, and only while they still name the scanned file: a full save would
            # write stale fields back (e.g. ``Organization.custom_domain``, #1199), and a file uploaded meanwhile
            # must stay. An image with derived images is removed with them from every row that names it (a
            # duplicated event shares the template's images).
            if THUMBNAIL_CONFIGS.get((meta.app_label, str(meta.model_name), field)):
                paths = detach_image_everywhere(path)
            else:
                model_class.objects.filter(pk=pk, **{field: path}).update(**{field: None})
                paths = [path]
            delete_unreferenced_files_after_commit(paths)
        else:
            # Non-nullable field: full_clean() would reject a blank value and roll back the
            # quarantine, so remove the file and the row outright instead. The row goes only while it still
            # names the scanned file: a replacement uploaded meanwhile keeps it. Lock and reload the row, then use the
            # instance's own delete(): some models clean up more there (QuestionnaireFile drops its thumbnails).
            default_storage.delete(path)
            current = model_class.objects.select_for_update().filter(pk=pk, **{field: path}).first()
            if current is not None:
                current.delete()
    # Notify users about malware detection
    notify_malware_detected.delay(app=app, model=model, pk=pk, field=field, file_hash=file_hash, findings=findings)
    return t.cast(dict[str, t.Any], findings)


# An audit younger than this is assumed to have its scan still queued or running. With a worker backlog
# longer than this, the sweep can queue a duplicate scan. That does no damage: a duplicate of a clean
# scan scans again, and a duplicate after a quarantine fails because the file or the row is gone.
RESCAN_GRACE = timedelta(minutes=15)
# ponytail: audits older than this are not dispatched again. This bounds the first run and stops the
# sweep from queuing for ever a file that never resolves its audit: a scan that keeps failing, or a
# file replaced without a new audit (its hash no longer matches, so the scan never updates the audit).
# Such a file is scanned every run until the cap. Upgrade path: a terminal audit status (e.g.
# SCAN_FAILED) set after N attempts, then drop the cap.
RESCAN_LOOKBACK = timedelta(days=7)


@shared_task(name="common.tasks.rescan_pending_uploads")
def rescan_pending_uploads() -> int:
    """Queue the malware scan again for uploads whose audit is still ``PENDING`` (#1446).

    The upload path queues the scan after commit. If the broker is down at that time, the file is
    stored but not scanned. This sweep picks the latest audit of each
    ``(app, model, instance_pk, field)`` that is still pending after ``RESCAN_GRACE``. For an audit
    without a stored path, a newer audit for the same field means that the file was replaced, so the
    sweep skips the older one, and it skips an instance or file that is gone. An audit with a stored
    path is always queued: the scan reads that path. Unknown models and fields are skipped.

    Returns:
        The number of scans queued.
    """
    if not settings.FEATURE_MALWARE_SCAN:
        return 0
    now = timezone.now()
    newer = FileUploadAudit.objects.filter(
        app=OuterRef("app"),
        model=OuterRef("model"),
        instance_pk=OuterRef("instance_pk"),
        field=OuterRef("field"),
        created_at__gt=OuterRef("created_at"),
    )
    # list(): materialize before dispatching (no server-side cursor behind PgBouncer).
    candidates = list(
        FileUploadAudit.objects.filter(
            status=FileUploadAudit.FileUploadAuditStatus.PENDING,
            created_at__lte=now - RESCAN_GRACE,
            created_at__gte=now - RESCAN_LOOKBACK,
        )
        # An audit with its own path is scanned even when superseded or when its row is gone: a copy such as a
        # duplicated event can still name that file. Older audits without a path keep the latest-only rule.
        .exclude(Q(file_path="") & Exists(newer))
        .values_list("app", "model", "instance_pk", "field", "file_path")
        .distinct()
    )
    queued = 0
    seen: set[tuple[t.Any, ...]] = set()
    for app, model, pk, field, file_path in candidates:
        if (app, model, pk, field) in seen:
            continue  # one scan covers every pending path of the upload
        try:
            model_class = apps.get_model(app, model)
            model_class._meta.get_field(field)
        except LookupError, FieldDoesNotExist:
            logger.warning("rescan_pending_upload_unknown_field", app=app, model=model, field=field)
            continue
        empty_file = Q(**{f"{field}__isnull": True}) | Q(**{field: ""})
        if not file_path and not model_class.objects.filter(pk=pk).exclude(empty_file).exists():
            continue
        seen.add((app, model, pk, field))
        scan_for_malware.delay(app=app, model=model, pk=str(pk), field=field)
        queued += 1
    if queued:
        logger.info("rescan_pending_uploads_queued", count=queued)
    return queued


def _get_organization_owner(app: str, model: str, pk: str) -> RevelUser | None:
    """Get organization owner for a given model instance.

    Returns None if:
    - Model/app doesn't exist (LookupError)
    - Instance doesn't exist (ObjectDoesNotExist)
    - Instance has no organization association
    """
    from django.core.exceptions import ObjectDoesNotExist

    try:
        model_class = apps.get_model(app, model)
        instance = model_class.objects.get(pk=pk)
    except LookupError, ObjectDoesNotExist:
        logger.debug("organization_owner_resolution_failed", app=app, model=model, pk=pk)
        return None

    # Check if the instance has an organization field
    if hasattr(instance, "organization") and instance.organization:
        return instance.organization.owner  # type: ignore[no-any-return]
    # Check if the instance itself is an organization
    elif model == "Organization":
        return instance.owner  # type: ignore[no-any-return]

    return None


@shared_task(name="common.tasks.notify_malware_detected")
def notify_malware_detected(
    *, app: str, model: str, pk: str, field: str, file_hash: str, findings: dict[str, t.Any]
) -> None:
    """Notify users when malware is detected.

    Notifies:
    - Django superusers and staff
    - Organization owner (if applicable)
    - File uploader
    """
    # Get the file upload audit record. Scope by app/model/field/instance (not just hash) so
    # neither a cross-model hash collision nor another instance's identical upload can pull in
    # an audit whose uploader/quarantine record don't belong to this ``pk``.
    audits = FileUploadAudit.objects.filter(
        app=app, model=model, field=field, file_hash=file_hash, instance_pk=pk, notified=False
    )

    for audit in audits:
        quarantined_file = QuarantinedFile.objects.filter(audit=audit).first()
        if quarantined_file is None:
            continue
        quarantined_file_url = reverse("admin:common_quarantinedfile_change", args=[quarantined_file.id])

        uploader = RevelUser.objects.filter(username=audit.uploader).first()

        # Get organization owner
        organization_owner = _get_organization_owner(app, model, pk)

        # Notify uploader
        if uploader:
            _notify_user_about_malware(uploader, _("Malware detected in your upload"))

        # Notify organization owner (if different from uploader)
        if organization_owner and organization_owner != uploader:
            _notify_user_about_malware(organization_owner, _("Malware detected in organization-related upload"))

        # Notify superusers and staff
        _notify_superusers_about_malware(
            app=app,
            model=model,
            pk=pk,
            field=field,
            findings=findings,
            uploader=uploader,
            quarantined_file_url=quarantined_file_url,
        )
        audit.notified = True
        audit.save(update_fields=["notified"])


def _notify_user_about_malware(user: RevelUser, subject: "str | Promise") -> None:
    """Send malware notification email to a user, in the user's language (workers have no active one)."""
    context = {"user": user, "frontend_base_url": SiteSettings.get_solo().frontend_base_url}
    with translation.override(user.language):
        subject = str(subject)
        txt_body = render_plain_text("common/emails/malware_detected_user.txt", context)
        html_body = render_to_string("common/emails/malware_detected_user.html", context)

    send_email.delay(
        to=user.email,
        subject=subject,
        body=txt_body,
        html_body=html_body,
    )


def _notify_superusers_about_malware(
    *,
    app: str,
    model: str,
    pk: str,
    field: str,
    findings: dict[str, t.Any],
    uploader: "RevelUser | None",
    quarantined_file_url: str,
) -> None:
    """Send malware notification email to superusers and staff."""
    User = get_user_model()
    superuser_emails = list(
        User.objects.filter(Q(is_superuser=True) | Q(is_staff=True)).values_list("email", flat=True).distinct()
    )

    if not superuser_emails:
        return

    subject = "Malware detected in file upload"
    context = {
        "uploader": uploader,
        "app": app,
        "model": model,
        "instance_pk": pk,
        "field": field,
        "findings": findings,
        "quarantined_file_url": quarantined_file_url,
        "frontend_base_url": SiteSettings.get_solo().frontend_base_url,
    }

    txt_body = render_plain_text("common/emails/malware_detected_superuser.txt", context)
    html_body = render_to_string("common/emails/malware_detected_superuser.html", context)

    send_email.delay(
        to=superuser_emails,
        subject=subject,
        body=txt_body,
        html_body=html_body,
    )


@shared_task(name="common.tasks.cleanup_expired_file_exports")
def cleanup_expired_file_exports() -> dict[str, int]:
    """Delete expired file exports (both files and database records).

    Export download links are valid for 7 days. After expiry, the records
    serve no purpose, so we delete both the stored file and the DB row.

    Also deletes FAILED exports older than the same window.

    Returns:
        Dict with count of records deleted.
    """
    from common.service.export_service import EXPORT_URL_EXPIRES_IN

    expiry_cutoff = timezone.now() - timedelta(seconds=EXPORT_URL_EXPIRES_IN)

    expired_exports = FileExport.objects.filter(
        Q(status=FileExport.ExportStatus.READY, completed_at__lte=expiry_cutoff)
        | Q(status=FileExport.ExportStatus.FAILED, updated_at__lte=expiry_cutoff)
    )

    count = 0
    for export in expired_exports:
        if export.file:
            export.file.delete(save=False)
        export.delete()
        count += 1
        logger.info("file_export_deleted", export_id=str(export.id))

    logger.info("file_export_cleanup_completed", records_deleted=count)
    return {"records_deleted": count}


@shared_task(
    name="common.fetch_exchange_rates",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=3600,
    max_retries=5,
)
def fetch_exchange_rates() -> dict[str, t.Any]:
    """Fetch daily exchange rates from frankfurter.dev and store them.

    Runs daily at 04:00 UTC via Celery beat. Rates are used for platform fee
    currency conversion and referral payout calculations.
    """
    from common.service.exchange_rate_service import fetch_and_store_rates

    exchange_rate = fetch_and_store_rates()
    return {"base": exchange_rate.base, "date": str(exchange_rate.date), "currencies": len(exchange_rate.rates)}
