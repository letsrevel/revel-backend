"""Celery tasks for attendee invoices, platform-fee invoices, VAT revalidation and referral payouts."""

import datetime
import typing as t
from uuid import UUID

import structlog
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from common.models import SiteSettings
from common.tasks import send_email
from events.models import Organization

if t.TYPE_CHECKING:
    from events.service.referral_payout_service import PayoutResult

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


class UndeliveredInvoiceSweepResult(t.TypedDict):
    """Telemetry counters returned by ``redispatch_undelivered_invoices_task``."""

    attendee_invoices: int
    attendee_credit_notes: int
    platform_fee_invoices: int


@shared_task(name="events.redispatch_undelivered_invoices")
def redispatch_undelivered_invoices_task() -> UndeliveredInvoiceSweepResult:
    """Backstop: re-dispatch financial documents whose email was never delivered.

    Unlike the generate→deliver chain (which only fires once), nothing revisits an
    ISSUED invoice / credit note whose delivery task exhausted its retries, leaving
    the document silently undelivered (issue #616). Selecting on ``email_sent_at``
    being null covers both failure modes: the deliver tasks self-heal a missing PDF
    before sending, so a document missing *either* the PDF *or* the email is
    recovered by re-dispatching delivery.

    Idempotent: ``mark_email_sent`` is a no-op once set, so a document already in
    flight is at worst delivered twice (at-least-once), never zero.

    Genuinely undeliverable documents (no recipient / org deleted) carry a terminal
    ``email_delivery_failed_at`` and are excluded so the sweep doesn't re-enqueue
    them every run forever (issue #618).
    """
    from events.models.attendee_invoice import AttendeeInvoice, AttendeeInvoiceCreditNote
    from events.models.invoice import PlatformFeeInvoice

    invoice_ids = list(
        AttendeeInvoice.objects.filter(
            status=AttendeeInvoice.InvoiceStatus.ISSUED,
            email_sent_at__isnull=True,
            email_delivery_failed_at__isnull=True,
        ).values_list("id", flat=True)
    )
    for invoice_id in invoice_ids:
        deliver_attendee_invoice_task.delay(str(invoice_id))

    credit_note_ids = list(
        AttendeeInvoiceCreditNote.objects.filter(
            email_sent_at__isnull=True,
            email_delivery_failed_at__isnull=True,
        ).values_list("id", flat=True)
    )
    for credit_note_id in credit_note_ids:
        deliver_attendee_credit_note_task.delay(str(credit_note_id))

    platform_fee_ids = list(
        PlatformFeeInvoice.objects.filter(
            status=PlatformFeeInvoice.InvoiceStatus.ISSUED,
            email_sent_at__isnull=True,
            email_delivery_failed_at__isnull=True,
        ).values_list("id", flat=True)
    )
    for platform_fee_id in platform_fee_ids:
        send_invoice_email_task.delay(str(platform_fee_id))

    result: UndeliveredInvoiceSweepResult = {
        "attendee_invoices": len(invoice_ids),
        "attendee_credit_notes": len(credit_note_ids),
        "platform_fee_invoices": len(platform_fee_ids),
    }
    if any(result.values()):
        logger.warning("undelivered_invoices_redispatched", **result)
    return result


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
    from events.service.invoice_service import ensure_invoice_pdf_exists, get_invoice_recipients

    invoice = PlatformFeeInvoice.objects.select_related("organization__owner").get(pk=invoice_id)
    org = invoice.organization
    if not org:
        logger.warning("invoice_org_deleted", invoice_number=invoice.invoice_number)
        invoice.mark_email_undeliverable(PlatformFeeInvoice.DeliveryFailureReason.ORG_DELETED)
        return

    recipients = get_invoice_recipients(org)
    if not recipients:
        logger.warning("no_invoice_recipients", invoice_number=invoice.invoice_number, org_id=str(org.id))
        invoice.mark_email_undeliverable(PlatformFeeInvoice.DeliveryFailureReason.NO_RECIPIENT)
        return

    # Self-heal a PDF lost to a crash between commit and PDF save (issue #616).
    ensure_invoice_pdf_exists(invoice)

    from django.template.loader import render_to_string

    site = SiteSettings.get_solo()
    bcc = [site.platform_invoice_bcc_email] if site.platform_invoice_bcc_email else []

    subject = _("Platform fee invoice %(invoice_number)s (%(currency)s)") % {
        "invoice_number": invoice.invoice_number,
        "currency": invoice.currency,
    }
    email_ctx = {
        "invoice_number": invoice.invoice_number,
        "currency": invoice.currency,
        "period_start": invoice.period_start.isoformat(),
        "period_end": invoice.period_end.isoformat(),
        "frontend_base_url": site.frontend_base_url,
    }
    body = render_to_string("events/emails/platform_fee_invoice_email.txt", email_ctx)
    html_body = render_to_string("events/emails/platform_fee_invoice_email.html", email_ctx)

    send_email(
        to=recipients,
        subject=subject,
        body=body,
        html_body=html_body,
        bcc=bcc,
        from_email=settings.DEFAULT_BILLING_EMAIL,
        reply_to=[settings.DEFAULT_REPLY_TO_EMAIL],
        attachment_storage_path=invoice.pdf_file.name,
        attachment_filename=f"{invoice.invoice_number}.pdf",
    )

    invoice.mark_email_sent()

    logger.info("invoice_email_sent", invoice_number=invoice.invoice_number, to=recipients)


class VatRevalidationResult(t.TypedDict):
    """Telemetry counters returned by ``revalidate_vat_ids_task``."""

    dispatched: int


@shared_task(name="events.revalidate_vat_ids")
def revalidate_vat_ids_task() -> VatRevalidationResult:
    """Dispatch per-org VIES re-validation tasks.

    Runs on the 15th of each month via Celery Beat.
    Each org gets its own task with independent retry.
    """
    org_ids = list(Organization.objects.filter(vat_id__gt="").values_list("id", flat=True))

    for org_id in org_ids:
        revalidate_single_vat_id_task.delay(str(org_id))

    logger.info("vat_revalidation_dispatched", org_count=len(org_ids))
    return {"dispatched": len(org_ids)}


@shared_task(
    name="events.revalidate_single_vat_id",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=3600,
    max_retries=5,
)
def revalidate_single_vat_id_task(org_id: str) -> None:
    """Re-validate a single organization's VAT ID via VIES.

    Retries with exponential backoff on VIES unavailability or network errors.
    Fails loudly on unexpected errors after max retries.
    """
    from events.service.vies_service import validate_and_update_organization

    org = Organization.objects.get(pk=org_id)
    if not org.vat_id:
        return

    validate_and_update_organization(org)
    logger.info("vat_revalidation_done", org_id=org_id, vat_id=org.vat_id, valid=org.vat_id_validated)


@shared_task(name="events.calculate_referral_payouts")
def calculate_referral_payouts() -> "PayoutResult":
    """Calculate referral earnings for the previous calendar month.

    Runs on the 1st of each month via Celery beat. For each Referral,
    aggregates platform fees from the referred user's organizations and creates
    a ReferralPayout record. Idempotent — safe to re-run.
    """
    import calendar

    today = timezone.now().date()
    # Previous month
    if today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1

    period_start = datetime.date(year, month, 1)
    period_end = datetime.date(year, month, calendar.monthrange(year, month)[1])

    from events.service.referral_payout_service import calculate_payouts_for_period

    result = calculate_payouts_for_period(period_start, period_end)
    logger.info("referral_payouts_calculated", period=str(period_start), **result)
    return result
