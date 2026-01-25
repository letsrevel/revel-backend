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
from django.core.files.base import ContentFile
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from common.models import EmailLog, FileUploadAudit, QuarantinedFile, SiteSettings
from common.thumbnails.tasks import (  # noqa: F401
    delete_orphaned_thumbnails_task,
    generate_thumbnails_task,
)

logger = structlog.get_logger(__name__)


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


@shared_task
def send_email(
    *,
    to: str | list[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    callback_data: dict[str, t.Any] | None = None,
) -> None:
    """Send an email with optional callback for delivery tracking.

    Args:
        to: Email address(es)
        subject: Email subject
        body: Plain text body
        html_body: HTML body (optional)
        callback_data: Optional callback configuration with:
            - module: Python module path (e.g., "accounts.tasks")
            - function: Function name to call (e.g., "mark_reminder_sent")
            - kwargs: Dict of keyword arguments to pass to the function
    """
    success = False
    error_message = None

    try:
        site_settings = SiteSettings.get_solo()
        recipients = [to] if isinstance(to, str) else to
        recipients = [to_safe_email_address(email, site_settings=site_settings) for email in recipients]

        # RFC 5322 requires a valid "To" header. For single recipients, use "to" directly.
        # For multiple recipients, use BCC to protect privacy and set "to" to the sender.
        if len(recipients) == 1:
            email_msg = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=recipients,
            )
        else:
            email_msg = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[settings.DEFAULT_FROM_EMAIL],
                bcc=recipients,
            )
        if html_body:  # pragma: no branch
            email_msg.attach_alternative(html_body, "text/html")
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


@shared_task
def cleanup_email_logs() -> None:
    """Clean up email logs."""
    older_than_a_week = EmailLog.objects.filter(sent_at__lte=timezone.now() - timedelta(days=7))
    older_than_a_week.delete()

    # delete compressed_body and compressed_html for older than a day
    older_than_a_day = EmailLog.objects.filter(sent_at__lte=timezone.now() - timedelta(days=1))
    older_than_a_day.update(compressed_body=None, compressed_html=None)


@shared_task
def dummy_add(a: int, b: int) -> int:
    """Dummy task to add two numbers."""
    return a + b


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


@shared_task
def scan_for_malware(*, app: str, model: str, pk: str, field: str) -> None | dict[str, t.Any]:
    """Scan for malware."""
    cd = pyclamd.ClamdNetworkSocket(host=settings.CLAMAV_HOST, port=settings.CLAMAV_PORT)
    if not cd.ping():
        raise RuntimeError("ClamAV daemon is not reachable")
    model_class = apps.get_model(app, model)
    instance = model_class.objects.get(pk=pk)
    file_field = getattr(instance, field)
    file_bytes = file_field.read()
    findings = cd.scan_stream(file_bytes)
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    audit_qs = FileUploadAudit.objects.filter(app=app, model=model, field=field, file_hash=file_hash)
    if not findings:
        audit_qs.update(status=FileUploadAudit.FileUploadAuditStatus.CLEAN, updated_at=timezone.now())
        return None
    quarantined = []
    filename = getattr(file_field, "name", file_hash)
    name = Path(filename).name
    for audit in audit_qs:
        quarantined.append(QuarantinedFile(audit=audit, file=ContentFile(file_bytes, name), findings=findings))
    with transaction.atomic():
        setattr(instance, field, None)
        audit_qs.update(status=FileUploadAudit.FileUploadAuditStatus.MALICIOUS, updated_at=timezone.now())
        QuarantinedFile.objects.bulk_create(quarantined)
        instance.save()
    # Notify users about malware detection
    notify_malware_detected.delay(app=app, model=model, pk=pk, field=field, file_hash=file_hash, findings=findings)
    return t.cast(dict[str, t.Any], findings)


def _get_organization_owner(app: str, model: str, pk: str) -> RevelUser | None:
    """Get organization owner for a given model instance."""
    try:
        model_class = apps.get_model(app, model)
        instance = model_class.objects.get(pk=pk)

        # Check if the instance has an organization field
        if hasattr(instance, "organization") and instance.organization:
            return instance.organization.owner  # type: ignore[no-any-return]
        # Check if the instance itself is an organization
        elif model == "Organization":
            return instance.owner  # type: ignore[no-any-return]
    except Exception:
        logger.debug("organization_owner_resolution_failed", app=app, model=model, pk=pk)
    return None


@shared_task
def notify_malware_detected(
    *, app: str, model: str, pk: str, field: str, file_hash: str, findings: dict[str, t.Any]
) -> None:
    """Notify users when malware is detected.

    Notifies:
    - Django superusers and staff
    - Organization owner (if applicable)
    - File uploader
    """
    # Get the file upload audit record
    audits = FileUploadAudit.objects.filter(file_hash=file_hash, notified=False)

    for audit in audits:
        quarantined_file = QuarantinedFile.objects.get(audit=audit)
        quarantined_file_url = reverse("admin:common_quarantinedfile_change", args=[quarantined_file.id])

        uploader = RevelUser.objects.filter(username=audit.uploader).first()

        # Get organization owner
        organization_owner = _get_organization_owner(app, model, pk)

        # Notify uploader
        if uploader:
            _notify_user_about_malware(uploader, "Malware detected in your upload")

        # Notify organization owner (if different from uploader)
        if organization_owner and organization_owner != uploader:
            _notify_user_about_malware(organization_owner, "Malware detected in organization-related upload")

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


def _notify_user_about_malware(user: RevelUser, subject: str) -> None:
    """Send malware notification email to a user."""
    context = {"user": user}
    txt_body = render_to_string("common/emails/malware_detected_user.txt", context)
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
    }

    txt_body = render_to_string("common/emails/malware_detected_superuser.txt", context)
    html_body = render_to_string("common/emails/malware_detected_superuser.html", context)

    send_email.delay(
        to=superuser_emails,
        subject=subject,
        body=txt_body,
        html_body=html_body,
    )
