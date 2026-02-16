"""Celery tasks for event management.

This module contains asynchronous tasks for:
- Building attendee visibility flags
- Cleaning up expired payments
- Resetting demo data
- Guest user confirmation emails
"""

from collections import Counter
from uuid import UUID

import structlog
from celery import shared_task
from django.core.management import call_command
from django.db import transaction
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from common.models import SiteSettings
from common.tasks import send_email

from .models import (
    AttendeeVisibilityFlag,
    Event,
    EventRSVP,
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
        ticket_count = Ticket.objects.filter(event=event, status=Ticket.TicketStatus.ACTIVE).count()
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
    attendees_q = Q(tickets__event=event, tickets__status=Ticket.TicketStatus.ACTIVE) | Q(
        rsvps__event=event, rsvps__status=EventRSVP.RsvpStatus.YES
    )

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


@shared_task(name="events.reset_demo_data")
def reset_demo_data() -> dict[str, str]:
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


@shared_task(name="events.cleanup_ticket_file_cache")
def cleanup_ticket_file_cache() -> dict[str, int]:
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
    for ticket in tickets_with_files.only("pk", "pdf_file", "pkpass_file").iterator():
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
