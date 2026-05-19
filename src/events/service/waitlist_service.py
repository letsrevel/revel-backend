"""Advanced waitlist processing.

See docs/superpowers/specs/2026-05-19-advanced-waitlist-design.md for the
full design. This module is the single entrypoint for offer-batch creation.
"""

from __future__ import annotations

import dataclasses
import random
import typing as t
import uuid

from django.db import transaction
from django.utils import timezone

from events.models import Event, EventWaitList, WaitlistOffer


@dataclasses.dataclass(frozen=True)
class ProcessResult:
    """Outcome of a process_waitlist_for_event call."""

    status: t.Literal[
        "disabled",
        "no_spots",
        "no_eligible_users",
        "cutoff_already_processed",
        "ok",
    ]
    offers_created: int = 0
    batch_id: uuid.UUID | None = None
    is_cutoff_batch: bool = False

    def as_dict(self) -> dict[str, t.Any]:
        """Return a JSON-serializable representation of the result."""
        return {
            "status": self.status,
            "offers_created": self.offers_created,
            "batch_id": str(self.batch_id) if self.batch_id else None,
            "is_cutoff_batch": self.is_cutoff_batch,
        }


@transaction.atomic
def process_waitlist_for_event(event_id: uuid.UUID) -> ProcessResult:
    """Create the next batch of waitlist offers for an event.

    Idempotent: concurrent invocations serialize on the Event row lock and the
    second invocation typically finds no_spots.

    Args:
        event_id: UUID of the event to process.

    Returns:
        ProcessResult describing what happened.
    """
    event = Event.objects.select_for_update().get(pk=event_id)
    if not event.waitlist_open or event.waitlist_time_window is None:
        return ProcessResult(status="disabled")

    now = timezone.now()
    # The Event row lock above serializes concurrent processing; we don't need
    # to additionally lock the offer rows here for the count.
    pending_count = WaitlistOffer.objects.filter(
        event=event, status=WaitlistOffer.Status.PENDING, expires_at__gt=now
    ).count()
    available = event.effective_capacity - event.attendee_count - pending_count
    if available <= 0:
        return ProcessResult(status="no_spots")

    past_cutoff = event.waitlist_cutoff_date is not None and now >= event.waitlist_cutoff_date
    if past_cutoff and WaitlistOffer.objects.filter(event=event, is_cutoff_batch=True).exists():
        return ProcessResult(status="cutoff_already_processed")

    waitlist_qs = (
        EventWaitList.objects.filter(event=event)
        .exclude(
            user__waitlist_offers__event=event,
            user__waitlist_offers__status=WaitlistOffer.Status.PENDING,
        )
        .select_related("user")
        .order_by("created_at")
    )

    if past_cutoff:
        selected = [w.user for w in waitlist_qs]
        expires_at = now + (event.waitlist_cutoff_window or event.waitlist_time_window)
        is_cutoff = True
    elif event.waitlist_batch_size == 0:
        selected = [w.user for w in waitlist_qs[:available]]
        expires_at = now + event.waitlist_time_window
        is_cutoff = False
    else:
        batch_count = min(event.waitlist_batch_size, available)
        if event.waitlist_lottery_mode:
            pool = list(waitlist_qs)
            selected = [w.user for w in random.sample(pool, min(batch_count, len(pool)))]
        else:
            selected = [w.user for w in waitlist_qs[:batch_count]]
        expires_at = now + event.waitlist_time_window
        is_cutoff = False

    if not selected:
        return ProcessResult(status="no_eligible_users")

    batch_id = uuid.uuid4()
    offers = WaitlistOffer.objects.bulk_create(
        [
            WaitlistOffer(
                event=event,
                user=u,
                expires_at=expires_at,
                batch_id=batch_id,
                is_cutoff_batch=is_cutoff,
            )
            for u in selected
        ]
    )

    offer_ids = [o.id for o in offers]
    transaction.on_commit(lambda: _dispatch_offer_notifications(offer_ids))

    return ProcessResult(
        status="ok",
        offers_created=len(offers),
        batch_id=batch_id,
        is_cutoff_batch=is_cutoff,
    )


@transaction.atomic
def revoke_all_pending_offers(event_id: uuid.UUID) -> int:
    """Mark every pending unexpired WaitlistOffer for an event as REVOKED.

    Used when the event is cancelled or its waitlist is closed. Already-expired
    rows in PENDING state are left untouched — the periodic sweeper transitions
    those to EXPIRED on its own schedule. Returns the number of rows updated.

    Args:
        event_id: UUID of the event whose pending offers should be revoked.

    Returns:
        The number of WaitlistOffer rows transitioned to REVOKED.
    """
    return (
        WaitlistOffer.objects.select_for_update()
        .filter(event_id=event_id, status=WaitlistOffer.Status.PENDING)
        .update(status=WaitlistOffer.Status.REVOKED)
    )


def enqueue_waitlist_processing(event_id: uuid.UUID) -> None:
    """Schedule waitlist processing after the current transaction commits.

    Safe to call multiple times in a single transaction; the underlying task
    is idempotent via the Event row lock.

    Args:
        event_id: UUID of the event to process.
    """
    from events.tasks import process_waitlist_for_event_task

    transaction.on_commit(lambda: process_waitlist_for_event_task.delay(str(event_id)))


def _dispatch_offer_notifications(offer_ids: list[uuid.UUID]) -> None:
    """Dispatch WAITLIST_SPOT_AVAILABLE notifications for fresh offers.

    The notification task is fully implemented in Task 20. Until then,
    `send_waitlist_offer_notification_task` is a no-op stub added in Task 7.

    Args:
        offer_ids: UUIDs of the WaitlistOffer rows to notify about.
    """
    from events.tasks import send_waitlist_offer_notification_task

    for offer_id in offer_ids:
        send_waitlist_offer_notification_task.delay(str(offer_id))
