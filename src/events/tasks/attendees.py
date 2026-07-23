"""Celery tasks for attendee visibility flags and guest confirmation emails."""

import hashlib

import structlog
from celery import shared_task
from django.db import connection, transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from common.models import SiteSettings
from common.tasks import send_email
from events.models import AttendeeVisibilityFlag, Event, EventRSVP, Ticket

logger = structlog.get_logger(__name__)


def visibility_rebuild_lock_key(event_id: str) -> int:
    """Derive a stable signed 64-bit Postgres advisory-lock key for a per-event visibility rebuild.

    The key is namespaced so it can't collide with other advisory-lock users, and is
    derived deterministically from the event UUID so every worker rebuilding the same
    event's matrix contends on the same lock.
    """
    digest = hashlib.sha256(f"events.attendee_visibility:{event_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@shared_task(name="events.tasks.build_attendee_visibility_flags")
def build_attendee_visibility_flags(event_id: str) -> None:
    """A task that builds flags for attendee visibility events.

    Optimized to use batch visibility resolution with prefetched data
    to avoid N+1 queries. Uses VisibilityContext for O(1) lookups.
    """
    from events.service.user_preferences_service import VisibilityContext, resolve_visibility_fast

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

    with transaction.atomic():
        # Serialize concurrent rebuilds of the same event's matrix. Every confirmed
        # ticket/RSVP dispatches this task, so a checkout rush runs several rebuilds
        # at once and the delete + bulk_create below interleave row locks across
        # workers and deadlock. A transaction-scoped advisory lock keyed on the event
        # serializes the rebuild without touching the Event row that checkout paths
        # select_for_update (so it adds no checkout latency). Released automatically
        # at commit/rollback.
        #
        # The lock is taken FIRST, before the visibility snapshot below: a task that
        # waits here must read the state left by the rebuild it waited on, otherwise
        # it would overwrite a newer matrix with stale flags.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [visibility_rebuild_lock_key(event_id)])

        # Re-fetch event without a row lock for visibility flag building (read-only)
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


@shared_task(name="events.tasks.send_guest_rsvp_confirmation")
def send_guest_rsvp_confirmation(email: str, token: str, event_name: str) -> None:
    """Send RSVP confirmation email to guest user.

    Args:
        email: Guest user's email
        token: JWT confirmation token
        event_name: Name of the event
    """
    logger.info("guest_rsvp_confirmation_sending", email=email, event_name=event_name)
    subject = _("Confirm your RSVP to %(event_name)s") % {"event_name": event_name}
    site_settings = SiteSettings.get_solo()
    confirmation_link = site_settings.frontend_base_url + f"/events/confirm-action?token={token}"
    ctx = {
        "confirmation_link": confirmation_link,
        "event_name": event_name,
        "frontend_base_url": site_settings.frontend_base_url,
    }
    body = render_to_string("events/emails/guest_rsvp_confirmation_body.txt", ctx)
    html_body = render_to_string("events/emails/guest_rsvp_confirmation_body.html", ctx)
    send_email(to=email, subject=subject, body=body, html_body=html_body)
    logger.info("guest_rsvp_confirmation_sent", email=email)


@shared_task(name="events.tasks.send_guest_ticket_confirmation")
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
    site_settings = SiteSettings.get_solo()
    confirmation_link = site_settings.frontend_base_url + f"/events/confirm-action?token={token}"
    ctx = {
        "confirmation_link": confirmation_link,
        "event_name": event_name,
        "tier_name": tier_name,
        "frontend_base_url": site_settings.frontend_base_url,
    }
    body = render_to_string("events/emails/guest_ticket_confirmation_body.txt", ctx)
    html_body = render_to_string("events/emails/guest_ticket_confirmation_body.html", ctx)
    send_email(to=email, subject=subject, body=body, html_body=html_body)
    logger.info("guest_ticket_confirmation_sent", email=email)
