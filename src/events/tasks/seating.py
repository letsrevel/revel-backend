"""Seat-hold hygiene. Correctness never depends on this task (spec §2)."""

from celery import shared_task
from django.utils import timezone

from events.models import SeatHold


@shared_task(name="events.cleanup_expired_seat_holds")
def cleanup_expired_seat_holds() -> int:
    """Delete expired seat holds. Availability/acquisition already filter/take over expired rows."""
    deleted, _ = SeatHold.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted
