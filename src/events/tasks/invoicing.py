"""Celery tasks for attendee invoices, credit notes, and platform-fee invoices."""

import typing as t
from uuid import UUID

import structlog
from celery import shared_task
from django.conf import settings
from django.utils.translation import gettext as _

from common.models import SiteSettings
from common.tasks import send_email

logger = structlog.get_logger(__name__)


@shared_task(
    name="events.generate_attendee_invoice",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=3,
)
def generate_attendee_invoice_task(stripe_session_id: str) -> None:
    """Generate an attendee invoice PDF for a completed checkout session.

    Runs asynchronously after payment success. On success, chains to the
    delivery task for AUTO-mode invoices.
    """
    from events.models.attendee_invoice import AttendeeInvoice
    from events.service.attendee_invoice_service import generate_attendee_invoice

    invoice = generate_attendee_invoice(stripe_session_id)
    if invoice and invoice.status == AttendeeInvoice.InvoiceStatus.ISSUED:
        deliver_attendee_invoice_task.delay(str(invoice.id))


@shared_task(
    name="events.deliver_attendee_invoice",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=3,
)
def deliver_attendee_invoice_task(invoice_id: str) -> None:
    """Deliver an attendee invoice via email."""
    from events.models.attendee_invoice import AttendeeInvoice
    from events.service.attendee_invoice_service import deliver_attendee_invoice

    invoice = AttendeeInvoice.objects.get(id=UUID(invoice_id))
    deliver_attendee_invoice(invoice)


@shared_task(
    name="events.generate_attendee_credit_note",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=3,
)
def generate_attendee_credit_note_task(stripe_session_id: str, refunded_payment_ids: list[str]) -> None:
    """Generate a credit note PDF for refunded payments on an invoiced session.

    On success, chains to the delivery task.
    """
    from events.service.attendee_invoice_service import generate_attendee_credit_note

    credit_note = generate_attendee_credit_note(stripe_session_id, [UUID(pid) for pid in refunded_payment_ids])
    if credit_note:
        deliver_attendee_credit_note_task.delay(str(credit_note.id))


@shared_task(
    name="events.deliver_attendee_credit_note",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=3,
)
def deliver_attendee_credit_note_task(credit_note_id: str) -> None:
    """Deliver a credit note via email."""
    from events.models.attendee_invoice import AttendeeInvoiceCreditNote
    from events.service.attendee_invoice_service import deliver_credit_note

    credit_note = AttendeeInvoiceCreditNote.objects.get(id=UUID(credit_note_id))
    deliver_credit_note(credit_note)


class MonthlyInvoiceGenerationResult(t.TypedDict):
    """Telemetry counters returned by ``generate_monthly_invoices_task``."""

    invoices_generated: int


@shared_task(name="events.generate_monthly_invoices")
def generate_monthly_invoices_task() -> MonthlyInvoiceGenerationResult:
    """Generate platform fee invoices for the previous month, then dispatch emails.

    Runs on the 1st of each month via Celery Beat.
    Invoice creation is idempotent (skips existing invoices).
    Each email is dispatched as a separate task for independent retry.
    """
    from events.service.invoice_service import generate_monthly_invoices

    invoices = generate_monthly_invoices()

    for invoice in invoices:
        if invoice.pdf_file and invoice.organization_id:
            send_invoice_email_task.delay(str(invoice.id))

    return {"invoices_generated": len(invoices)}


@shared_task(
    name="events.send_invoice_email",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=3600,
    max_retries=5,
)
def send_invoice_email_task(invoice_id: str) -> None:
    """Send a single platform fee invoice email.

    Separate task so each email can retry independently on SMTP failure
    without affecting other invoices.
    """
    from events.models.invoice import PlatformFeeInvoice
    from events.service.invoice_service import get_invoice_recipients

    invoice = PlatformFeeInvoice.objects.select_related("organization__owner").get(pk=invoice_id)
    org = invoice.organization
    if not org:
        logger.warning("invoice_org_deleted", invoice_number=invoice.invoice_number)
        return

    recipients = get_invoice_recipients(org)
    if not recipients:
        logger.warning("no_invoice_recipients", invoice_number=invoice.invoice_number, org_id=str(org.id))
        return

    site = SiteSettings.get_solo()
    bcc = [site.platform_invoice_bcc_email] if site.platform_invoice_bcc_email else []

    subject = _("Platform fee invoice %(invoice_number)s (%(currency)s)") % {
        "invoice_number": invoice.invoice_number,
        "currency": invoice.currency,
    }
    body = _(
        "Please find attached the platform fee invoice %(invoice_number)s "
        "for %(currency)s transactions in the period %(period_start)s to %(period_end)s."
    ) % {
        "invoice_number": invoice.invoice_number,
        "currency": invoice.currency,
        "period_start": invoice.period_start.isoformat(),
        "period_end": invoice.period_end.isoformat(),
    }

    send_email(
        to=recipients,
        subject=subject,
        body=body,
        bcc=bcc,
        from_email=settings.DEFAULT_BILLING_EMAIL,
        reply_to=[settings.DEFAULT_REPLY_TO_EMAIL],
        attachment_storage_path=invoice.pdf_file.name,
        attachment_filename=f"{invoice.invoice_number}.pdf",
    )

    logger.info("invoice_email_sent", invoice_number=invoice.invoice_number, to=recipients)
