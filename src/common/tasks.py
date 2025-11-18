"""Common tasks."""

import hashlib
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

logger = structlog.get_logger(__name__)


@shared_task
def send_email(*, to: str | list[str], subject: str, body: str, html_body: str | None = None) -> None:
    """Send an email with a token link.

    Args:
        to (str): The email address.
        subject (str): The email subject.
        body (str): The email body.
        html_body (str | None): The HTML email body.

    Returns:
        None
    """
    site_settings = SiteSettings.get_solo()
    recipients = [to] if isinstance(to, str) else to
    recipients = [to_safe_email_address(email, site_settings=site_settings) for email in recipients]
    email_msg = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        bcc=recipients,
    )
    if html_body:  # pragma: no branch
        email_msg.attach_alternative(html_body, "text/html")
    email_msg.send(fail_silently=False)
    email_logs: list[EmailLog] = []
    for recipient in recipients:
        el = EmailLog(to=recipient, subject=subject)
        el.set_body(body=body)
        if html_body:  # pragma: no branch
            el.set_html(html_body=html_body)
        email_logs.append(el)
    EmailLog.objects.bulk_create(email_logs)


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
        audit_qs.update(status=FileUploadAudit.Status.CLEAN, updated_at=timezone.now())
        return None
    quarantined = []
    filename = getattr(file_field, "name", file_hash)
    name = Path(filename).name
    for audit in audit_qs:
        quarantined.append(QuarantinedFile(audit=audit, file=ContentFile(file_bytes, name), findings=findings))
    with transaction.atomic():
        setattr(instance, field, None)
        audit_qs.update(status=FileUploadAudit.Status.MALICIOUS, updated_at=timezone.now())
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
        pass
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
