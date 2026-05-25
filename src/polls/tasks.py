"""Celery tasks for polls."""

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from polls.models import Poll


@shared_task(name="polls.tasks.close_polls_due")
def close_polls_due() -> int:
    """Close any polls whose ``closes_at`` has elapsed.

    Streams candidate IDs via ``.iterator()`` so the task footprint does not
    grow with the number of due polls. Each transition is wrapped in its own
    ``transaction.atomic`` so a slow row does not hold a long-running
    transaction over the whole batch.

    Returns:
        The number of polls that were transitioned from OPEN to CLOSED.
    """
    now = timezone.now()
    due_ids_qs = (
        Poll.objects.filter(status=Poll.PollStatus.OPEN, closes_at__lte=now)
        .values_list("id", flat=True)
        .iterator(chunk_size=100)
    )
    closed = 0
    for poll_id in due_ids_qs:
        with transaction.atomic():
            locked = (
                Poll.objects.select_for_update()
                .filter(pk=poll_id, status=Poll.PollStatus.OPEN, closes_at__lte=timezone.now())
                .first()
            )
            if locked is None:
                continue
            locked.status = Poll.PollStatus.CLOSED
            locked.closed_at = timezone.now()
            locked.save(update_fields=["status", "closed_at", "updated_at"])
            closed += 1
    return closed
