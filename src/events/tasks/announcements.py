"""Celery tasks for scheduled and resent organization announcements.

The tasks carry explicit registered names (``events.send_scheduled_announcements``
and ``events.resend_announcements_to_new_signups``), so the Celery-beat schedules
defined in migration 0082 — which reference tasks by name string — are unaffected.
"""

import typing as t

import structlog
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from events.models import Announcement

logger = structlog.get_logger(__name__)


class ScheduledAnnouncementResult(t.TypedDict):
    """Telemetry for ``send_scheduled_announcements``."""

    sent: int


@shared_task(name="events.send_scheduled_announcements")
def send_scheduled_announcements() -> ScheduledAnnouncementResult:
    """Send scheduled announcements whose effective time has arrived.

    Runs every 5 minutes via Celery beat (migration 0082). Snapshots candidate
    IDs, then re-fetches each under ``select_for_update`` (PgBouncer rule, #458),
    recomputes the live effective time (relative schedules auto-shift with the
    event), and sends the due ones. ``send_announcement``'s own status check makes
    a double-send by overlapping beat runs harmless.
    """
    from events.service import announcement_service

    now = timezone.now()
    ids = list(Announcement.objects.scheduled().values_list("id", flat=True))
    sent = 0
    for ann_id in ids:
        with transaction.atomic():
            try:
                ann = Announcement.objects.select_for_update().select_related("organization").get(pk=ann_id)
            except Announcement.DoesNotExist:
                continue
            if ann.status != Announcement.AnnouncementStatus.SCHEDULED:
                continue
            effective = ann.effective_send_at
            if effective is None or effective > now:
                continue
            announcement_service.send_announcement(ann)
            sent += 1
    logger.info("scheduled_announcements_swept", sent=sent, candidates=len(ids))
    return {"sent": sent}


class ResendAnnouncementResult(t.TypedDict):
    """Telemetry for ``resend_announcements_to_new_signups``."""

    resent: int
    recipients: int


@shared_task(name="events.resend_announcements_to_new_signups")
def resend_announcements_to_new_signups() -> ResendAnnouncementResult:
    """Re-deliver sent announcements to attendees who joined after the first send.

    Runs every 15 minutes via Celery beat (migration 0082). Only event
    announcements with ``resend_to_new_signups=True`` whose event has not ended
    are considered; the stop condition is purely the query. Snapshots IDs then
    processes each under ``select_for_update`` (PgBouncer rule, #458).
    """
    from events.service import announcement_service

    now = timezone.now()
    ids = list(
        Announcement.objects.filter(
            status=Announcement.AnnouncementStatus.SENT,
            resend_to_new_signups=True,
            event__end__gt=now,
        ).values_list("id", flat=True)
    )
    resent = 0
    recipients = 0
    for ann_id in ids:
        with transaction.atomic():
            try:
                ann = Announcement.objects.select_for_update().select_related("organization").get(pk=ann_id)
            except Announcement.DoesNotExist:
                continue
            if ann.status != Announcement.AnnouncementStatus.SENT or not ann.resend_to_new_signups:
                continue
            n = announcement_service.resend_to_new_recipients(ann)
            if n:
                resent += 1
                recipients += n
    logger.info("announcements_resent_swept", resent=resent, recipients=recipients, candidates=len(ids))
    return {"resent": resent, "recipients": recipients}
