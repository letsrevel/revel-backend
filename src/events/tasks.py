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
from events.service import update_db_instance

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
    """A task that builds flags for attendee visibility events."""
    from .service.user_preferences_service import resolve_visibility

    event = Event.objects.with_organization().get(pk=event_id)

    # Users attending the event

    attendees_q = Q(tickets__event=event, tickets__status=Ticket.TicketStatus.ACTIVE) | Q(
        rsvps__event=event, rsvps__status=EventRSVP.RsvpStatus.YES
    )

    attendees = RevelUser.objects.filter(attendees_q).distinct()

    update_db_instance(event, attendee_count=attendees.count())

    # Users invited or attending = potential viewers
    viewers = RevelUser.objects.filter(Q(invitations__event=event) | attendees_q).distinct()

    flags = []

    organization = event.organization
    owner_id = organization.owner_id
    staff_ids = {sm.id for sm in organization.staff_members.all()}

    with transaction.atomic():
        AttendeeVisibilityFlag.objects.filter(event=event).delete()
        for viewer in viewers:
            for target in attendees:
                visible = resolve_visibility(viewer, target, event, owner_id, staff_ids)
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
    from common.tasks import send_email

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
    from common.tasks import send_email

    logger.info("guest_ticket_confirmation_sending", email=email, event_name=event_name, tier_name=tier_name)
    subject = _("Confirm your ticket for %(event_name)s") % {"event_name": event_name}
    confirmation_link = SiteSettings.get_solo().frontend_base_url + f"/events/confirm-action?token={token}"
    body = render_to_string(
        "events/emails/guest_ticket_confirmation_body.txt",
        {"confirmation_link": confirmation_link, "event_name": event_name, "tier_name": tier_name},
    )
    send_email(to=email, subject=subject, body=body)
    logger.info("guest_ticket_confirmation_sent", email=email)
