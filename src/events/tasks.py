"""Celery tasks for event management.

This module contains asynchronous tasks for:
- Building attendee visibility flags
- Cleaning up expired payments
- Resetting demo data
- Guest user confirmation emails
"""

import datetime
import typing as t
from collections import Counter
from uuid import UUID

import httpx
import structlog
from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.db import OperationalError as DjangoOperationalError
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext as _
from kombu.exceptions import OperationalError as KombuOperationalError

from accounts.models import RevelUser
from common.models import SiteSettings
from common.tasks import send_email

if t.TYPE_CHECKING:
    from events.service.referral_payout_service import PayoutResult

from .models import (
    AttendeeVisibilityFlag,
    Event,
    EventRSVP,
    MembershipSubscription,
    Organization,
    OrganizationContactMessage,
    Payment,
    Ticket,
    TicketTier,
)

logger = structlog.get_logger(__name__)


@shared_task
def build_attendee_visibility_flags(event_id: str) -> None:
    """A task that builds flags for attendee visibility events.

    Optimized to use batch visibility resolution with prefetched data
    to avoid N+1 queries. Uses VisibilityContext for O(1) lookups.
    """
    from .service.user_preferences_service import VisibilityContext, resolve_visibility_fast

    # Update attendee count atomically with a lock to prevent race conditions.
    # Multiple tasks may run concurrently when tickets are confirmed rapidly;
    # this ensures the count is read and written while holding the lock.
    with transaction.atomic():
        event = Event.objects.with_organization().select_for_update().get(pk=event_id)
        ticket_count = Ticket.objects.filter(
            event=event,
            status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN],
        ).count()
        rsvp_count = EventRSVP.objects.filter(event=event, status=EventRSVP.RsvpStatus.YES).count()
        event.attendee_count = ticket_count + rsvp_count
        event.save(update_fields=["attendee_count"])

    # Re-fetch event without lock for visibility flag building (read-only operations)
    event = Event.objects.with_organization().get(pk=event_id)

    organization = event.organization
    owner_id = organization.owner_id
    staff_ids = {sm.id for sm in organization.staff_members.all()}

    # Pre-load all relationship data in 4 queries (instead of N queries per pair)
    context = VisibilityContext.for_event(event, owner_id, staff_ids)

    # Users attending the event (for visibility purposes)
    # Prefetch general_preferences to avoid N+1 when accessing target.general_preferences
    attendees_q = Q(
        tickets__event=event,
        tickets__status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN],
    ) | Q(rsvps__event=event, rsvps__status=EventRSVP.RsvpStatus.YES)

    attendees = list(RevelUser.objects.filter(attendees_q).select_related("general_preferences").distinct())

    # Users invited or attending = potential viewers
    viewers = list(RevelUser.objects.filter(Q(invitations__event=event) | attendees_q).distinct())

    flags = []

    with transaction.atomic():
        AttendeeVisibilityFlag.objects.filter(event=event).delete()
        for viewer in viewers:
            for target in attendees:
                # O(1) visibility check using prefetched context
                visible = resolve_visibility_fast(viewer, target, context)
                flags.append(
                    AttendeeVisibilityFlag(
                        user=viewer,
                        target=target,
                        event=event,
                        is_visible=visible,
                    )
                )

        AttendeeVisibilityFlag.objects.bulk_create(
            flags,
            update_conflicts=True,
            update_fields=["is_visible"],
            unique_fields=["user", "event", "target"],
        )


@shared_task(name="events.cleanup_expired_payments")
def cleanup_expired_payments() -> int:
    """Finds and deletes expired payments that are still in a 'pending' state.

    Releases their associated ticket reservation by decrementing the tier's
    quantity_sold counter.
    This task is idempotent and safe to run periodically.
    """
    # Find payments for tickets that are still pending and whose Stripe session has expired.
    expired_payments_qs = Payment.objects.filter(
        status=Payment.PaymentStatus.PENDING, expires_at__lt=timezone.now()
    ).select_related("ticket", "ticket__tier")

    if not expired_payments_qs.exists():
        return 0

    # Collect IDs and tier counts before the transaction to avoid holding locks for too long
    payment_ids_to_delete = list(expired_payments_qs.values_list("id", flat=True))
    ticket_ids_to_delete = list(expired_payments_qs.values_list("ticket_id", flat=True))
    tickets_to_release_by_tier: Counter[UUID] = Counter(
        expired_payments_qs.filter(ticket__tier_id__isnull=False).values_list("ticket__tier_id", flat=True)
    )

    logger.info(
        f"Found {len(payment_ids_to_delete)} expired payments to clean up "
        f"across {len(tickets_to_release_by_tier)} tiers."
    )

    with transaction.atomic():
        # Atomically decrement the quantity_sold for each affected tier.
        for tier_id, count_to_release in tickets_to_release_by_tier.items():
            TicketTier.objects.select_for_update().filter(pk=tier_id).update(
                quantity_sold=F("quantity_sold") - count_to_release
            )

        # Delete payments first due to PROTECT constraint on Ticket
        Payment.objects.filter(pk__in=payment_ids_to_delete).delete()

        # Now delete the associated pending tickets
        Ticket.objects.filter(pk__in=ticket_ids_to_delete, status=Ticket.TicketStatus.PENDING).delete()

    logger.info(f"Successfully cleaned up {len(payment_ids_to_delete)} expired payments.")
    return len(payment_ids_to_delete)


class ResetDemoDataResult(t.TypedDict):
    """Status payload returned by ``reset_demo_data``."""

    status: str
    message: str


@shared_task(name="events.reset_demo_data")
def reset_demo_data() -> ResetDemoDataResult:
    """Reset demo data by deleting organizations and example.com users, then re-bootstrapping.

    This task invokes the reset_events management command with --no-input flag.
    Only runs when DEMO_MODE is enabled.

    Returns:
        Dictionary with status information.
    """
    logger.info("Starting demo data reset task...")
    call_command("reset_events", "--no-input")
    logger.info("Demo data reset completed successfully")
    return {"status": "success", "message": "Demo data has been reset"}


@shared_task
def send_guest_rsvp_confirmation(email: str, token: str, event_name: str) -> None:
    """Send RSVP confirmation email to guest user.

    Args:
        email: Guest user's email
        token: JWT confirmation token
        event_name: Name of the event
    """
    logger.info("guest_rsvp_confirmation_sending", email=email, event_name=event_name)
    subject = _("Confirm your RSVP to %(event_name)s") % {"event_name": event_name}
    confirmation_link = SiteSettings.get_solo().frontend_base_url + f"/events/confirm-action?token={token}"
    body = render_to_string(
        "events/emails/guest_rsvp_confirmation_body.txt",
        {"confirmation_link": confirmation_link, "event_name": event_name},
    )
    send_email(to=email, subject=subject, body=body)
    logger.info("guest_rsvp_confirmation_sent", email=email)


@shared_task
def send_guest_ticket_confirmation(email: str, token: str, event_name: str, tier_name: str) -> None:
    """Send ticket purchase confirmation email to guest user.

    Only sent for non-online-payment tickets (free/offline/at-the-door).

    Args:
        email: Guest user's email
        token: JWT confirmation token
        event_name: Name of the event
        tier_name: Name of the ticket tier
    """
    logger.info("guest_ticket_confirmation_sending", email=email, event_name=event_name, tier_name=tier_name)
    subject = _("Confirm your ticket for %(event_name)s") % {"event_name": event_name}
    confirmation_link = SiteSettings.get_solo().frontend_base_url + f"/events/confirm-action?token={token}"
    body = render_to_string(
        "events/emails/guest_ticket_confirmation_body.txt",
        {"confirmation_link": confirmation_link, "event_name": event_name, "tier_name": tier_name},
    )
    send_email(to=email, subject=subject, body=body)
    logger.info("guest_ticket_confirmation_sent", email=email)


class TicketFileCacheCleanupResult(t.TypedDict):
    """Telemetry counters returned by ``cleanup_ticket_file_cache``."""

    cleaned: int


@shared_task(name="events.cleanup_ticket_file_cache")
def cleanup_ticket_file_cache() -> TicketFileCacheCleanupResult:
    """Delete cached PDF/pkpass files for tickets whose events have ended.

    Frees storage for past events since cached files are no longer needed.
    Files can always be regenerated on demand if needed.

    Returns:
        Dict with count of cleaned tickets.
    """
    now = timezone.now()
    tickets_with_files = Ticket.objects.filter(
        event__end__lt=now,
    ).filter(Q(pdf_file__gt="") | Q(pkpass_file__gt=""))

    cleaned_pks: list[UUID] = []
    for ticket in tickets_with_files.only("pk", "pdf_file", "pkpass_file"):
        try:
            if ticket.pdf_file:
                ticket.pdf_file.delete(save=False)
            if ticket.pkpass_file:
                ticket.pkpass_file.delete(save=False)
            cleaned_pks.append(ticket.pk)
        except OSError:
            logger.warning("Failed to clean cached files for ticket %s", ticket.pk, exc_info=True)

    if cleaned_pks:
        Ticket.objects.filter(pk__in=cleaned_pks).update(
            pdf_file="",
            pkpass_file="",
            file_content_hash=None,
        )
        logger.info("cleanup_ticket_file_cache_done", cleaned=len(cleaned_pks))

    return {"cleaned": len(cleaned_pks)}


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


class RecurringEventGenerationResult(t.TypedDict):
    """Telemetry counters returned by ``generate_recurring_events_task``."""

    series_dispatched: int


@shared_task(name="events.generate_recurring_events")
def generate_recurring_events_task() -> RecurringEventGenerationResult:
    """Maintain the rolling generation window for all active recurring series.

    Runs daily via Celery Beat (scheduled by migration 0067). Dispatches a
    per-series subtask for each active series so that a failure on one series
    does not block the others, and each generation runs in its own atomic
    transaction. Idempotent — safe to re-run (skips already-existing
    occurrences).

    The per-series dispatch is wrapped in a narrow try/except for transport
    failures only (broker disconnect, queue declaration errors). Programming
    errors in the subtask itself propagate through the broker and are
    surfaced via the subtask's own retry/failure semantics — they should
    never be silently swallowed by the dispatcher.
    """
    from events.models import EventSeries

    series_ids = list(
        EventSeries.objects.filter(
            recurrence_rule__isnull=False,
            template_event__isnull=False,
            is_active=True,
        ).values_list("id", flat=True)
    )

    dispatched = 0
    for series_id in series_ids:
        try:
            generate_single_series_events_task.delay(str(series_id))
        except KombuOperationalError:
            # Broker is unreachable or refused the publish — log and continue
            # so one bad publish doesn't strand the rest of the batch. The
            # next daily run will retry. Anything other than a transport
            # failure should propagate.
            logger.exception("recurring_events_dispatch_failed", series_id=str(series_id))
            continue
        dispatched += 1

    # Report *successful* publishes, not just attempted ones, so a partial
    # broker outage doesn't look like full success in metrics.
    logger.info(
        "recurring_events_generation_dispatched",
        series_attempted=len(series_ids),
        series_dispatched=dispatched,
    )
    return {"series_dispatched": dispatched}


@shared_task(
    name="events.generate_single_series_events",
    autoretry_for=(DjangoOperationalError, DatabaseError),
    retry_backoff=60,
    retry_backoff_max=3600,
    max_retries=3,
)
def generate_single_series_events_task(series_id: str) -> int:
    """Generate rolling-window events for a single recurring series.

    Dispatched by :func:`generate_recurring_events_task`. Retries with
    exponential backoff only on transient database failures (deadlocks,
    connection drops). Programming errors (missing template, invalid rule,
    etc.) fail loudly on the first run instead of burning ~70 minutes of
    backoff before surfacing.

    Returns:
        The number of events created for this series.
    """
    from events.models import EventSeries
    from events.service.recurrence_service import generate_series_events

    series = (
        EventSeries.objects.select_related(
            "recurrence_rule",
            "template_event",
            "organization",
            "organization__owner",
        )
        .prefetch_related("organization__staff_members")
        .get(pk=series_id)
    )
    created = generate_series_events(series)
    logger.info(
        "recurring_events_series_generated",
        series_id=series_id,
        events_created=len(created),
    )
    return len(created)


@shared_task
def generate_questionnaire_export_task(export_id: str) -> None:
    """Generate an Excel export of questionnaire submissions."""
    from events.service.export.questionnaire_export import generate_questionnaire_export

    generate_questionnaire_export(UUID(export_id))


@shared_task
def generate_attendee_export_task(export_id: str) -> None:
    """Generate an Excel export of event attendees."""
    from events.service.export.attendee_export import generate_attendee_export

    generate_attendee_export(UUID(export_id))


@shared_task
def send_organization_contact_email_verification(
    email: str, token: str, organization_name: str, organization_slug: str
) -> None:
    """Send organization contact email verification.

    Args:
        email: The new contact email to verify
        token: JWT verification token
        organization_name: Name of the organization
        organization_slug: Slug of the organization
    """
    logger.info(
        "organization_contact_email_verification_sending",
        email=email,
        organization_name=organization_name,
    )
    subject = _("Verify contact email for %(organization_name)s") % {"organization_name": organization_name}
    verification_link = (
        SiteSettings.get_solo().frontend_base_url + f"/org/{organization_slug}/verify-contact-email?token={token}"
    )
    body = render_to_string(
        "events/emails/organization_contact_email_verification_body.txt",
        {
            "verification_link": verification_link,
            "organization_name": organization_name,
            "contact_email": email,
        },
    )
    html_body = render_to_string(
        "events/emails/organization_contact_email_verification_body.html",
        {
            "verification_link": verification_link,
            "organization_name": organization_name,
            "contact_email": email,
        },
    )
    send_email(to=email, subject=subject, body=body, html_body=html_body)
    logger.info("organization_contact_email_verification_sent", email=email)


@shared_task
def send_organization_contact_message_email(message_id: str) -> None:
    """Forward a contact-form submission to an organization's contact mailbox.

    Sends one email with ``Reply-To`` set to the sender so the org can reply
    directly. The org's own ``contact_email`` MUST be verified — the resolver
    invariant guarantees that contact_method=FORM implies a verified email.
    """
    message = OrganizationContactMessage.objects.select_related("organization").get(pk=UUID(message_id))
    organization = message.organization
    if not (organization.contact_email and organization.contact_email_verified):
        logger.warning(
            "organization_contact_message_skipped_unverified",
            message_id=message_id,
            organization_id=str(organization.id),
        )
        return

    subject_text = message.subject.strip() or _("New contact message")
    subject = f"[{organization.name}] {subject_text}"
    admin_link = (
        SiteSettings.get_solo().frontend_base_url + f"/org/{organization.slug}/admin/contact-messages/{message.id}"
    )
    body = render_to_string(
        "events/emails/organization_contact_message_body.txt",
        {
            "organization_name": organization.name,
            "sender_email": message.sender_email_snapshot,
            "subject": message.subject,
            "message": message.message,
            "admin_link": admin_link,
        },
    )
    html_body = render_to_string(
        "events/emails/organization_contact_message_body.html",
        {
            "organization_name": organization.name,
            "sender_email": message.sender_email_snapshot,
            "subject": message.subject,
            "message": message.message,
            "admin_link": admin_link,
        },
    )
    send_email(
        to=organization.contact_email,
        subject=subject,
        body=body,
        html_body=html_body,
        reply_to=[message.sender_email_snapshot],
    )
    logger.info(
        "organization_contact_message_sent",
        message_id=message_id,
        organization_id=str(organization.id),
    )


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


@shared_task(bind=True, max_retries=3)
def notify_admin_new_organization_pushover(self: t.Any, organization_id: str) -> dict[str, t.Any]:
    """Send a Pushover notification to admin when a new organization is created.

    Includes the org name, owner's email, and the running total of organizations.
    Logs a warning and returns a skipped status if Pushover is not configured.
    """
    if not settings.PUSHOVER_USER_KEY or not settings.PUSHOVER_APP_TOKEN:
        logger.warning(
            "pushover_not_configured",
            message="PUSHOVER_USER_KEY or PUSHOVER_APP_TOKEN not set in settings",
        )
        return {"status": "skipped", "reason": "pushover_not_configured"}

    org = Organization.objects.select_related("owner").get(id=organization_id)
    org_count = Organization.objects.count()
    message = f"New organization: {org.name}\nOwner: {org.owner.email}\nWe now have {org_count} organizations!"
    payload = {
        "token": settings.PUSHOVER_APP_TOKEN,
        "user": settings.PUSHOVER_USER_KEY,
        "message": message,
        "title": "New Organization",
        "priority": 0,
    }

    try:
        response = httpx.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10.0)
        response.raise_for_status()
        logger.info(
            "pushover_notification_sent",
            channel="organization_created",
            organization_id=organization_id,
            response_status=response.status_code,
        )
        return {"status": "sent", "organization_id": organization_id}
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
        logger.error("pushover_error", organization_id=organization_id, error=str(e))
        if self.request.retries < self.max_retries:
            countdown = 2**self.request.retries * 60
            raise self.retry(exc=e, countdown=countdown)
        logger.exception("pushover_exception", organization_id=organization_id, error=str(e))
        raise


@shared_task(bind=True, max_retries=3)
def notify_admin_new_organization_discord(self: t.Any, organization_id: str) -> dict[str, t.Any]:
    """Send a Discord notification when a new organization is created.

    Includes the org name, owner's email, and running total. Mentions are
    neutralized via ``allowed_mentions.parse=[]`` so that an org name
    containing ``@everyone`` or a role mention cannot trigger pings.
    """
    webhook_url = settings.DISCORD_ADMIN_WEBHOOK_URL
    if not webhook_url:
        logger.info("discord_webhook_not_configured")
        return {"status": "skipped", "reason": "discord_webhook_not_configured"}

    org = Organization.objects.select_related("owner").get(id=organization_id)
    org_count = Organization.objects.count()
    payload = {
        "content": (
            f"🏛️ New organization created: **{org.name}** (owner: {org.owner.email}). "
            f"We now have {org_count} organizations."
        ),
        "allowed_mentions": {"parse": []},
    }

    try:
        response = httpx.post(webhook_url, json=payload, timeout=10.0)
        response.raise_for_status()
        logger.info(
            "discord_notification_sent",
            channel="organization_created",
            organization_id=organization_id,
            org_count=org_count,
            response_status=response.status_code,
        )
        return {"status": "sent", "organization_id": organization_id, "org_count": org_count}
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
        logger.error("discord_error", channel="organization_created", organization_id=organization_id, error=str(e))
        if self.request.retries < self.max_retries:
            countdown = 2**self.request.retries * 60
            raise self.retry(exc=e, countdown=countdown)
        logger.exception(
            "discord_exception", channel="organization_created", organization_id=organization_id, error=str(e)
        )
        raise


class SubscriptionExpiryCounters(t.TypedDict):
    """Telemetry counters returned by ``expire_subscriptions_past_grace``."""

    expired_immediate: int
    past_due: int
    expired_after_grace: int


@shared_task(name="events.expire_subscriptions_past_grace")
def expire_subscriptions_past_grace() -> SubscriptionExpiryCounters:
    """Advance membership subscriptions through their lifecycle.

    Runs daily via Celery beat (see migration 0070). Transitions:

    1. ``ACTIVE`` lapsed with ``cancel_at_period_end=True`` → ``EXPIRED``.
    2. ``ACTIVE`` lapsed otherwise → ``PAST_DUE``.
    3. ``PAST_DUE`` past the org's grace window → ``EXPIRED``.

    Each row is locked with ``select_for_update`` and its preconditions
    are re-checked inside the lock so concurrent ``record_payment`` /
    cancellation calls cannot be clobbered. Rows are processed individually
    so the ``post_save`` signal fires and syncs ``OrganizationMember``.
    """
    now = timezone.now()
    counters: SubscriptionExpiryCounters = {"expired_immediate": 0, "past_due": 0, "expired_after_grace": 0}

    # 1 + 2: lapsed ACTIVE → EXPIRED (if cancel_at_period_end) else PAST_DUE.
    # list(), not .iterator(): a server-side cursor can't survive the per-row
    # commits below under PgBouncer transaction pooling (see #458).
    active_lapsed_ids = MembershipSubscription.objects.filter(
        status=MembershipSubscription.SubscriptionStatus.ACTIVE,
        current_period_end__lt=now,
    ).values_list("id", flat=True)
    for sub_id in list(active_lapsed_ids):
        with transaction.atomic():
            sub = (
                MembershipSubscription.objects.select_for_update()
                .select_related("plan", "plan__tier", "organization")
                .get(pk=sub_id)
            )
            # Re-check inside the lock — a concurrent record_payment may have
            # renewed the period or cancelled the subscription since the
            # snapshot was taken.
            if (
                sub.status != MembershipSubscription.SubscriptionStatus.ACTIVE
                or sub.current_period_end is None
                or sub.current_period_end >= now
            ):
                continue
            if sub.cancel_at_period_end:
                sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
                sub.cancelled_at = sub.cancelled_at or now
                sub.save(update_fields=["status", "cancelled_at", "updated_at"])
                counters["expired_immediate"] += 1
            else:
                sub.status = MembershipSubscription.SubscriptionStatus.PAST_DUE
                sub.save(update_fields=["status", "updated_at"])
                counters["past_due"] += 1

    # 3: PAST_DUE past grace → EXPIRED (list() not .iterator(), see #458).
    past_due_ids = MembershipSubscription.objects.filter(
        status=MembershipSubscription.SubscriptionStatus.PAST_DUE,
        current_period_end__isnull=False,
    ).values_list("id", flat=True)
    for sub_id in list(past_due_ids):
        with transaction.atomic():
            sub = (
                MembershipSubscription.objects.select_for_update()
                .select_related("plan", "plan__tier", "organization")
                .get(pk=sub_id)
            )
            if sub.status != MembershipSubscription.SubscriptionStatus.PAST_DUE or sub.current_period_end is None:
                continue
            grace_days = sub.organization.membership_grace_period_days
            if sub.current_period_end + datetime.timedelta(days=grace_days) >= now:
                continue
            sub.status = MembershipSubscription.SubscriptionStatus.EXPIRED
            sub.cancelled_at = sub.cancelled_at or now
            sub.save(update_fields=["status", "cancelled_at", "updated_at"])
            counters["expired_after_grace"] += 1

    logger.info("expire_subscriptions_past_grace_done", **counters)
    return counters


@shared_task(name="events.process_waitlist_for_event")
def process_waitlist_for_event_task(event_id: str) -> dict[str, t.Any]:
    """Celery wrapper for ``waitlist_service.process_waitlist_for_event``."""
    from events.service.waitlist_service import process_waitlist_for_event

    result = process_waitlist_for_event(UUID(event_id))
    return result.as_dict()


@shared_task(name="events.expire_waitlist_offers")
def expire_waitlist_offers_task() -> dict[str, t.Any]:
    """Flip expired PENDING offers to EXPIRED and trigger next batches.

    Hourly Beat schedule. Read paths defensively filter ``expires_at > now``,
    so the flip cadence affects only the timing of the next-batch enqueue.
    """
    from events.models import WaitlistOffer

    now = timezone.now()
    with transaction.atomic():
        expiring = WaitlistOffer.objects.select_for_update().filter(
            status=WaitlistOffer.WaitlistOfferStatus.PENDING, expires_at__lte=now
        )
        # PostgreSQL rejects `FOR UPDATE` with `DISTINCT`, so we materialize the
        # locked rows' event_ids and de-duplicate in Python.
        event_ids = list(set(expiring.values_list("event_id", flat=True)))
        count = expiring.update(status=WaitlistOffer.WaitlistOfferStatus.EXPIRED)

    for event_id in event_ids:
        process_waitlist_for_event_task.delay(str(event_id))

    logger.info("expire_waitlist_offers_done", expired=count, events_processed=len(event_ids))
    return {"expired": count, "events_processed": len(event_ids)}


@shared_task(name="events.nudge_open_waitlists")
def nudge_open_waitlists_task() -> dict[str, t.Any]:
    """Enqueue process_waitlist_for_event for every event with an active advanced waitlist.

    Hourly safety net against soft-locks where a batch fully claims without a follow-up trigger.
    The processor is idempotent (Event row lock + ``available <= 0`` early-return).
    """
    from events.models import Event
    from events.service.waitlist_service import enqueue_waitlist_processing

    event_ids = list(
        Event.objects.filter(
            waitlist_open=True,
            waitlist_time_window__isnull=False,
        ).values_list("id", flat=True)
    )
    for event_id in event_ids:
        enqueue_waitlist_processing(event_id)

    logger.info("nudge_open_waitlists_done", events_nudged=len(event_ids))
    return {"events_nudged": len(event_ids)}


@shared_task(name="events.send_waitlist_offer_notification")
def send_waitlist_offer_notification_task(offer_id: str) -> dict[str, t.Any]:
    """Dispatch WAITLIST_SPOT_AVAILABLE for a single offer. Transactional class (mirrors TICKET_CREATED)."""
    from uuid import UUID as _UUID

    from django.contrib.humanize.templatetags.humanize import naturaltime

    from events.models import WaitlistOffer
    from events.utils import format_event_datetime, get_event_timezone
    from notifications.enums import NotificationType
    from notifications.signals import notification_requested

    try:
        offer = WaitlistOffer.objects.select_related("user", "event__organization").get(pk=_UUID(offer_id))
    except WaitlistOffer.DoesNotExist:
        logger.warning("send_waitlist_offer_notification_missing", offer_id=offer_id)
        return {"status": "skipped", "offer_id": offer_id}

    if offer.status != WaitlistOffer.WaitlistOfferStatus.PENDING:
        logger.info("send_waitlist_offer_notification_non_pending", offer_id=offer_id, status=offer.status)
        return {"status": "skipped", "offer_id": offer_id}

    if offer.expires_at <= timezone.now():
        # Race vs sweeper: PENDING but already past expiry — don't pester user.
        logger.info("send_waitlist_offer_notification_expired", offer_id=offer_id)
        return {"status": "skipped", "offer_id": offer_id}

    site_settings = SiteSettings.get_solo()
    event_tz = get_event_timezone(offer.event)
    expires_local = offer.expires_at.astimezone(event_tz)
    start_local = offer.event.start.astimezone(event_tz) if offer.event.start else None

    context = {
        "event_id": str(offer.event_id),
        "event_name": offer.event.name,
        "event_start": start_local.isoformat() if start_local else "",
        "event_start_formatted": format_event_datetime(offer.event.start, offer.event),
        "event_url": f"{site_settings.frontend_base_url}/events/{offer.event.slug}",
        "organization_id": str(offer.event.organization_id),
        "organization_name": offer.event.organization.name,
        "offer_id": str(offer.id),
        "expires_at": expires_local.isoformat(),
        "expires_at_formatted": format_event_datetime(offer.expires_at, offer.event),
        "time_remaining_formatted": str(naturaltime(offer.expires_at)),
        "is_cutoff_batch": offer.is_cutoff_batch,
    }

    notification_requested.send(
        sender=WaitlistOffer,
        user=offer.user,
        notification_type=NotificationType.WAITLIST_SPOT_AVAILABLE,
        context=context,
    )

    offer.notified_at = timezone.now()
    offer.save(update_fields=["notified_at"])

    logger.info("send_waitlist_offer_notification_dispatched", offer_id=offer_id)
    return {"status": "sent", "offer_id": offer_id}
