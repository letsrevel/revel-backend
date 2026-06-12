"""Celery tasks for the advanced waitlist (offer expiry, processing, notifications)."""

import typing as t
from uuid import UUID

import structlog
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from common.models import SiteSettings

logger = structlog.get_logger(__name__)


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
